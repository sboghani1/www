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

from .claude_token import AUTH_ENV, is_token_line
from .config import DEFAULT_MODEL, CommonConfig
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
CREDENTIALS = Path("/home/receptionist-agent/.claude/.credentials.json")
RECEPTIONIST2_BIN = "/opt/receptionist2/current/.venv/bin"
# decision-helper-bot shares receptionist-agent's Claude login but reads
# auth.env only when it starts (EnvironmentFile=).
AUTH_CONSUMER_UNITS = ("decision-helper-bot.service",)


def read_auth_env_token() -> str:
    try:
        lines = AUTH_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    token = ""
    # Like systemd's EnvironmentFile=, the last assignment wins.
    for line in lines:
        if is_token_line(line):
            token = line.split("=", 1)[1].strip().strip("'\"")
    return token


def read_oauth_token() -> str:
    token = read_auth_env_token()
    if token:
        return token
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
    models = database.configured_models() if database is not None else []
    auth = auth_status(models[0] if models else DEFAULT_MODEL)
    logged_in = auth["logged_in"]
    status = {
        "condition": (
            "ok"
            if logged_in is True
            else "auth_failed"
            if logged_in is False
            else "unknown"
        ),
        "logged_in": logged_in,
        "auth_method": auth["auth_method"],
        "detail": auth_detail(auth),
    }
    token = read_oauth_token()
    status["models"] = {
        model: probe_model(token, model)
        if token
        else {"condition": "unknown", "detail": "OAuth token unavailable"}
        for model in models
    }
    return status


def auth_status(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    # Agent runs prefer the long-lived token in auth.env over the login in
    # .credentials.json, so a present token is what has to be valid.
    token = read_auth_env_token()
    if token:
        return token_auth_status(token, model)
    return cli_auth_status()


def token_auth_status(token: str, model: str) -> dict[str, Any]:
    probe = probe_model(token, model)
    condition = probe["condition"]
    logged_in = (
        True
        if condition in ("ok", "blocked")
        else False
        if condition == "auth_failed"
        else None
    )
    return {
        "logged_in": logged_in,
        "auth_method": "oauth_token",
        "detail": probe["detail"],
    }


def cli_auth_status() -> dict[str, Any]:
    try:
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
    except subprocess.TimeoutExpired:
        return {
            "logged_in": None,
            "auth_method": "unknown",
            "detail": "claude auth status timed out",
        }
    # `claude auth status` exits 1 when logged out but still prints its JSON.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    logged_in = payload.get("loggedIn")
    return {
        "logged_in": logged_in if isinstance(logged_in, bool) else None,
        "auth_method": str(payload.get("authMethod", "unknown")),
        "detail": result.stderr.strip()[-500:],
    }


def auth_detail(auth: dict[str, Any]) -> str:
    if auth["logged_in"] is True:
        return "Claude authentication is valid"
    if auth["logged_in"] is False and auth["auth_method"] == "oauth_token":
        return (
            f"The long-lived token in {AUTH_ENV} was rejected "
            f"({auth.get('detail') or 'no detail'}). "
            "Send /reauth to the watchdog bot."
        )
    if auth["logged_in"] is False:
        return (
            f"Claude is logged out: {AUTH_ENV} has no token and the login in "
            f"{CREDENTIALS} is gone or expired. Send /reauth to the watchdog bot."
        )
    return str(auth.get("detail") or "Claude authentication status is unknown")



def reauth_command() -> list[str]:
    # The watchdog's mount namespace has a read-only /home, so the token
    # session runs in a transient unit outside it to write auth.env.
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
        f"{RECEPTIONIST2_BIN}/receptionist2-claude-token",
    ]


def apply_auth() -> dict[str, Any]:
    restarted: list[str] = []
    for unit in AUTH_CONSUMER_UNITS:
        if run(["/usr/bin/systemctl", "is-active", "--quiet", unit]).returncode:
            continue
        result = run(["/usr/bin/systemctl", "restart", unit], timeout=120)
        if result.returncode == 0:
            restarted.append(unit)
    return {"restarted": restarted}


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
        choices=(
            "status",
            "logs",
            "provider",
            "restart",
            "auth-status",
            "reauth",
            "apply-auth",
        ),
    )
    parser.add_argument("request_id", nargs="?")
    args = parser.parse_args()
    if args.action == "reauth":
        command = reauth_command()
        os.execv(command[0], command)
    if args.action == "auth-status":
        print(json.dumps(auth_status()))
        return
    if args.action == "apply-auth":
        print(json.dumps(apply_auth()))
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
