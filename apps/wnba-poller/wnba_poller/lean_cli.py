from __future__ import annotations

import fcntl
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .config import Config
from .lean_context import build_lean_context
from .lean_workflow import GitPublisher, execute_revision
from .sheets import SheetsStore

MAX_REQUEST_BYTES = 131_072


def _repository_root(cwd: Path) -> Path:
    current = cwd.resolve()
    path210 = current / "apps/wnba-poller/path210.md"
    git_dir = current / ".git"
    if not git_dir.exists() or not path210.is_file():
        raise ValueError("run WNBA lean workflow from the www repository root")
    return current


def _required_text(
    request: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{key} is invalid")
    return value


def handle_request(
    request: Mapping[str, Any],
    *,
    store: SheetsStore,
    repository: Path,
    now: datetime,
) -> dict[str, Any]:
    action = request.get("action")
    event_id = _required_text(request, "event_id", maximum=128)
    matchup = _required_text(request, "matchup", maximum=200)
    path210_path = repository / "apps/wnba-poller/path210.md"
    if action == "context":
        return build_lean_context(
            store,
            event_id=event_id,
            expected_matchup=matchup,
            path210_document=path210_path.read_text(encoding="utf-8"),
            now=now,
        )
    if action == "apply":
        operation = _required_text(request, "operation", maximum=20)
        request_text = _required_text(
            request, "request_text", maximum=5000
        )
        output = request.get("output")
        if output is not None and not isinstance(output, Mapping):
            raise ValueError("output must be an object or null")
        dry_run = bool(request.get("dry_run", False))
        lock_path = repository / ".git/wnba-lean.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "another WNBA lean workflow is running"
                ) from exc
            return execute_revision(
                store=store,
                publisher=GitPublisher(repository=repository),
                path210_path=path210_path,
                event_id=event_id,
                expected_matchup=matchup,
                operation=operation,
                request_text=request_text,
                source="claude-skill",
                now=now,
                output=output,
                target_revision_id=str(
                    request.get("target_revision_id") or ""
                ),
                dry_run=dry_run,
            )
    raise ValueError("unsupported WNBA lean workflow action")


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        print(json.dumps({"ok": False, "error": "request too large"}))
        raise SystemExit(2)
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        repository = _repository_root(Path.cwd())
        config = Config.from_env(require_google=True)
        store = SheetsStore.connect(
            sheet_id=config.sheet_id,
            credentials_b64=config.google_credentials_b64,
            service_account_json=config.google_service_account_json,
        )
        result = handle_request(
            request,
            store=store,
            repository=repository,
            now=datetime.now(tz=UTC),
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": (str(exc) or type(exc).__name__)[:1000]}
            )
        )
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
