from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import CommonConfig
from .database import Database

GATEWAY_SERVICE = "receptionist2-gateway.service"
WORKER_SERVICE = "receptionist2-worker.service"


def run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def unit_status(unit: str) -> dict[str, str]:
    result = run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,MainPID,NRestarts",
        ]
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return {
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", "unknown"),
        "pid": values.get("MainPID", "unknown"),
        "restarts": values.get("NRestarts", "unknown"),
    }


CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
AUTH_ENV = Path("/home/receptionist-agent/.claude/auth.env")
CREDENTIALS = Path("/home/receptionist-agent/.claude/.credentials.json")


def read_oauth_token() -> str:
    if AUTH_ENV.is_file():
        for line in AUTH_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    if CREDENTIALS.is_file():
        try:
            payload = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
            token = payload.get("claudeAiOauth", {}).get("accessToken", "")
            if isinstance(token, str):
                return token.strip()
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def probe_model(token: str, model: str) -> dict[str, str]:
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
            "system": CLAUDE_CODE_SYSTEM,
        }
    ).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"condition": "ok", "detail": f"HTTP {response.status}"}
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            payload = json.loads(error.read())
            detail = str(payload.get("error", {}).get("message", ""))
        except (json.JSONDecodeError, AttributeError):
            pass
        condition = (
            "blocked"
            if error.code == 429
            else "auth_failed"
            if error.code in (401, 403)
            else "unknown"
        )
        return {
            "condition": condition,
            "detail": f"HTTP {error.code}: {detail or 'request rejected'}",
        }
    except (OSError, TimeoutError) as error:
        return {"condition": "unknown", "detail": str(error)}


def provider_status(database: Database | None = None) -> dict[str, Any]:
    result = run(
        [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            "/usr/bin/claude",
            "auth",
            "status",
        ],
        timeout=45,
    )
    payload: dict[str, Any] = {}
    if result.returncode == 0:
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            pass
    logged_in = payload.get("loggedIn")
    status = {
        "condition": (
            "ok"
            if logged_in is True
            else "auth_failed"
            if logged_in is False
            else "unknown"
        ),
        "logged_in": logged_in,
        "auth_method": payload.get("authMethod", "unknown"),
        "detail": (
            result.stderr.strip()[-500:]
            if result.returncode
            else "Claude authentication is valid"
        ),
    }
    token = read_oauth_token()
    models = database.configured_models() if database is not None else []
    status["models"] = {
        model: probe_model(token, model)
        if token
        else {"condition": "unknown", "detail": "OAuth token unavailable"}
        for model in models
    }
    return status


def auth_status() -> dict[str, Any]:
    result = run(
        [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            "receptionist-agent",
            "/usr/bin/claude",
            "auth",
            "status",
        ],
        timeout=45,
    )
    try:
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except json.JSONDecodeError:
        payload = {}
    logged_in = payload.get("loggedIn") if isinstance(payload, dict) else None
    return {
        "logged_in": logged_in if isinstance(logged_in, bool) else None,
        "auth_method": (
            payload.get("authMethod", "unknown")
            if isinstance(payload, dict)
            else "unknown"
        ),
    }


def reauth_command() -> list[str]:
    # The watchdog's mount namespace has a read-only /home, so the login must
    # run in a transient unit outside it to persist Claude credentials.
    return [
        "/usr/bin/systemd-run",
        "--pipe",
        "--wait",
        "--quiet",
        "--collect",
        f"--unit=receptionist2-reauth-{int(time.time())}",
        "--uid=receptionist-agent",
        "--gid=receptionist-agent",
        "--working-directory=/home/receptionist-agent",
        "--setenv=HOME=/home/receptionist-agent",
        "--setenv=USER=receptionist-agent",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "/usr/bin/claude",
        "auth",
        "login",
    ]


def status(database: Database) -> dict[str, Any]:
    worker_heartbeat = database.get_state("worker_heartbeat", "")
    try:
        heartbeat_age = max(0.0, time.time() - float(worker_heartbeat))
    except ValueError:
        heartbeat_age = None
    return {
        "schema": "receptionist2_status_v1",
        "gateway": unit_status(GATEWAY_SERVICE),
        "worker": unit_status(WORKER_SERVICE),
        "queue": database.queue_summary(),
        "database": {"quick_check": quick_check(database.path)},
        "provider": provider_status(database),
        "readiness": {
            "gateway": database.get_state("gateway_status", "unknown"),
            "worker": database.get_state("worker_status", "unknown"),
            "worker_heartbeat_age_seconds": heartbeat_age,
        },
        "draining": database.get_state("draining", "0") == "1",
        "recent_runs": database.recent_runs(5),
    }


def quick_check(path: Path) -> str:
    try:
        with Database(path).connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except Exception as error:
        return f"error: {error}"
    return str(row[0]) if row else "no result"


def logs() -> dict[str, Any]:
    result = run(
        [
            "/usr/bin/journalctl",
            "-u",
            GATEWAY_SERVICE,
            "-u",
            WORKER_SERVICE,
            "--since=-2 hours",
            "-n",
            "200",
            "--no-pager",
            "--output=short-iso",
        ]
    )
    lines = result.stdout.splitlines()[-40:]
    return {"schema": "receptionist2_logs_v1", "lines": lines}


def restart(database: Database, request_id: str) -> None:
    request = database.get_restart_request(request_id)
    if request["status"] not in {"pending", "running"}:
        raise RuntimeError("Restart request is no longer executable")
    database.set_state("draining", "1")
    timeout = request["timeout_seconds"]
    started = time.monotonic()
    try:
        while database.running_runs():
            if timeout is not None and time.monotonic() - started >= int(timeout):
                break
            time.sleep(2)
        database.set_state("gateway_status", "restarting")
        database.set_state("worker_status", "restarting")
        database.set_state("worker_heartbeat", "")
        result = run(
            [
                "/usr/bin/systemctl",
                "restart",
                WORKER_SERVICE,
                GATEWAY_SERVICE,
            ],
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "systemctl restart failed")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            gateway = unit_status(GATEWAY_SERVICE)
            worker = unit_status(WORKER_SERVICE)
            worker_heartbeat = database.get_state("worker_heartbeat", "")
            try:
                heartbeat_recent = (
                    time.time() - float(worker_heartbeat) <= 30
                )
            except ValueError:
                heartbeat_recent = False
            if (
                gateway["active"] == "active"
                and worker["active"] == "active"
                and database.get_state("gateway_status", "") == "ready"
                and database.get_state("worker_status", "") == "ready"
                and heartbeat_recent
            ):
                database.finish_restart_request(request_id)
                return
            time.sleep(3)
        raise RuntimeError(
            "Services did not become application-ready within 90 seconds"
        )
    except Exception as error:
        database.finish_restart_request(request_id, error=str(error))
        raise
    finally:
        database.set_state("draining", "0")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("status", "logs", "provider", "restart", "auth-status", "reauth"),
    )
    parser.add_argument("request_id", nargs="?")
    args = parser.parse_args()
    if args.action == "reauth":
        command = reauth_command()
        os.execv(command[0], command)
    if args.action == "auth-status":
        print(json.dumps(auth_status()))
        return
    config = CommonConfig.from_env()
    database = Database(config.database_path)
    database.initialize()
    if args.action == "status":
        print(json.dumps(status(database)))
    elif args.action == "logs":
        print(json.dumps(logs()))
    elif args.action == "provider":
        print(json.dumps(provider_status(database)))
    else:
        if not args.request_id:
            raise SystemExit("restart requires request_id")
        restart(database, args.request_id)


if __name__ == "__main__":
    main()
