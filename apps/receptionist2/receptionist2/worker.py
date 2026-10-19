from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from .config import WorkerConfig
from .context import bootstrap_prompt, build_context_manifest
from .database import Database
from .deployments import (
    diagnose_markup,
    is_self_deployment,
    parse_request_file,
    started_text,
)
from .investigation import continuation_prompt, missing_sections
from .nfl_audio import remove_audio_files
from .provider import (
    ProviderResult,
    build_command,
    consume_event,
    provider_error_message,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("receptionist2.worker")
PROGRESS_INTERVAL_SECONDS = 3
DEPLOY_DRAIN_STALE_SECONDS = 20 * 60


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Worker:
    def __init__(self, config: WorkerConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self._stopping = asyncio.Event()
        self._active_process: asyncio.subprocess.Process | None = None
        self._deployment_lock = asyncio.Lock()
        self._deployment_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        self.database.initialize()
        self.database.set_state("worker_status", "starting")
        await self.reconcile()
        self._cleanup_pending_nfl_audio()
        self.finish_self_deployment()
        self.database.set_state("worker_status", "ready")
        heartbeat = asyncio.create_task(self._heartbeat())
        deployments = asyncio.create_task(self._deployment_loop())
        try:
            while not self._stopping.is_set():
                if self.database.running_runs():
                    await self.reconcile()
                    if self.database.running_runs():
                        await self._sleep(self.config.poll_seconds)
                        continue
                run = self.database.claim_next_run()
                if run is None:
                    await self._sleep(self.config.poll_seconds)
                    continue
                try:
                    await self.execute(run)
                except Exception as error:
                    log.exception("Run %s crashed in the worker", run["id"])
                    if self.database.get_run(run["id"])["status"] == "running":
                        self._finish_run(
                            run["id"],
                            status="failed",
                            error=f"Worker error: {type(error).__name__}",
                        )
        finally:
            for task in (heartbeat, deployments, *self._deployment_tasks):
                task.cancel()
            await asyncio.gather(
                heartbeat,
                deployments,
                *self._deployment_tasks,
                return_exceptions=True,
            )
            self.database.set_state("worker_status", "stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def _heartbeat(self) -> None:
        while True:
            self.database.set_state("worker_heartbeat", str(time.time()))
            self._cleanup_pending_nfl_audio()
            await asyncio.sleep(10)

    async def reconcile(self) -> None:
        for run in self.database.running_runs():
            pid = run.get("process_id")
            if pid and process_alive(int(pid)):
                log.warning(
                    "Run %s still has a live process %s; leaving it running",
                    run["id"],
                    pid,
                )
                continue
            receipt = self.config.run_state_dir / f"{run['id']}.receipt.json"
            recovered = self._read_receipt(receipt)
            if recovered and recovered.get("final_response"):
                self._finish_run(
                    run["id"],
                    status=(
                        "failed" if recovered.get("is_error") else "succeeded"
                    ),
                    final_response=str(recovered["final_response"]),
                    error=str(recovered.get("error") or ""),
                    provider_session_id=(
                        str(recovered["session_id"])
                        if recovered.get("session_id")
                        else None
                    ),
                    usage=(
                        recovered.get("usage")
                        if isinstance(recovered.get("usage"), dict)
                        else {}
                    ),
                )
            else:
                self._finish_run(
                    run["id"],
                    status="interrupted",
                    error=(
                        "The worker restarted before a complete provider "
                        "receipt was available. The run was not repeated "
                        "automatically to avoid duplicating side effects. "
                        "Send /recover to retry it."
                    ),
                )

    @staticmethod
    def _read_receipt(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _prepare_prompt(self, run: dict[str, Any]) -> str | None:
        prompt = run["exact_prompt"]
        if run["source"] == "schedule":
            prompt = (
                "Read and follow "
                "`www/apps/receptionist2/skills/receptionist2-recurring-task/"
                "SKILL.md` before executing this scheduled occurrence.\n\n"
                f"SCHEDULED_FOR={run['scheduled_for']}\n\n{prompt}"
            )
        if run.get("provider_session_id"):
            return prompt
        manifest = build_context_manifest(
            self.database,
            user_id=int(run["telegram_user_id"]),
            repo_root=self.config.repo_root,
            timezone=str(self.config.timezone),
        )
        if any(
            source.get("status") != "loaded"
            for source in manifest["sources"].values()
        ):
            return None
        return bootstrap_prompt(prompt, manifest)

    async def execute(self, run: dict[str, Any]) -> None:
        prompt = self._prepare_prompt(run)
        if prompt is None:
            self._finish_run(
                run["id"],
                status="failed",
                error="Required startup context could not be loaded.",
            )
            return
        command = build_command(
            self.config.claude_binary,
            prompt,
            run["model"],
            run["effort"],
            run.get("provider_session_id"),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/sudo",
                "-n",
                "-H",
                "-u",
                "receptionist-agent",
                self.config.agent_launcher,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=16 * 1024 * 1024,
            )
        except OSError as error:
            self._finish_run(
                run["id"], status="failed", error=f"Launch failed: {error}"
            )
            return
        self._active_process = process
        self.database.set_run_process(run["id"], process.pid)
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "repository": str(self.config.repo_root),
                    "command": command,
                    "run_id": run["id"],
                }
            ).encode()
        )
        await process.stdin.drain()
        process.stdin.close()

        result = ProviderResult()
        stderr_task = asyncio.create_task(self._read_stderr(process))
        cancel_task = asyncio.create_task(self._watch_cancel(run["id"], process))
        timed_out = False
        last_progress = 0.0
        try:
            assert process.stdout is not None
            async with asyncio.timeout(self.config.run_timeout_seconds):
                while line := await process.stdout.readline():
                    try:
                        changed = consume_event(
                            line.decode(errors="replace"), result
                        )
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        log.warning(
                            "Ignored malformed provider event for %s", run["id"]
                        )
                        continue
                    now = time.monotonic()
                    if changed and now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                        last_progress = now
                        self.database.update_run_progress(
                            run["id"],
                            activity=result.activity,
                            current_work=result.current_work,
                        )
                exit_code = await process.wait()
        except TimeoutError:
            timed_out = True
            await self._terminate(process)
            exit_code = process.returncode
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            stderr = await stderr_task
            self._active_process = None

        common = {
            "provider_session_id": result.session_id,
            "usage": result.usage,
            "context_tokens": result.context_tokens,
            "context_window_tokens": result.context_window_tokens,
        }
        if self.database.cancel_requested(run["id"]):
            self._finish_run(
                run["id"],
                status="cancelled",
                error="Stopped from Telegram.",
                **common,
            )
            return
        if timed_out:
            self._finish_run(
                run["id"],
                status="failed",
                error=(
                    "Provider run timed out after "
                    f"{self.config.run_timeout_seconds // 60} minutes."
                ),
                **common,
            )
            return
        if exit_code == 0 and result.final_response and not result.is_error:
            self._finish_run(
                run["id"],
                status="succeeded",
                final_response=result.final_response,
                **common,
            )
            self._continue_investigation(run, result.final_response)
            return
        self._finish_run(
            run["id"],
            status="failed",
            final_response=result.final_response,
            error=(
                provider_error_message(result)
                or stderr[-2000:]
                or result.final_response[-2000:]
                or f"Claude exited with {exit_code}"
            ),
            **common,
        )

    def _finish_run(
        self, run_id: str, *, status: str, **values: Any
    ) -> None:
        self.database.finish_run(run_id, status=status, **values)
        paths = self.database.nfl_audio_paths_for_run(run_id)
        if not paths:
            return
        errors = remove_audio_files(paths)
        if errors:
            log.error(
                "Could not remove NFL audio for run %s: %s",
                run_id,
                ", ".join(errors),
            )
            return
        self.database.mark_nfl_audio_run_cleaned(
            run_id, succeeded=status == "succeeded"
        )

    def _cleanup_pending_nfl_audio(self) -> None:
        for collection in self.database.pending_nfl_audio_cleanup():
            if collection["run_id"]:
                paths = self.database.nfl_audio_paths_for_run(
                    collection["run_id"]
                )
            else:
                paths = [
                    item["local_path"]
                    for item in self.database.nfl_audio_items(collection["id"])
                    if item["local_path"]
                ]
            errors = remove_audio_files(paths)
            if errors:
                log.error(
                    "Could not remove NFL audio collection %s: %s",
                    collection["id"],
                    ", ".join(errors),
                )
                continue
            if collection["run_id"]:
                self.database.mark_nfl_audio_run_cleaned(
                    collection["run_id"],
                    succeeded=collection["run_status"] == "succeeded",
                )
            else:
                self.database.mark_nfl_audio_collection_cleaned(
                    collection["id"]
                )

    def _continue_investigation(
        self, run: dict[str, Any], response: str
    ) -> None:
        if run["source"] != "investigation" or run.get("continuation_of"):
            return
        missing = missing_sections(response)
        if not missing:
            return
        continuation = self.database.enqueue_run(
            session_id=run["session_id"],
            user_id=int(run["telegram_user_id"]),
            chat_id=int(run["telegram_chat_id"]),
            source="investigation",
            prompt=continuation_prompt(missing),
            continuation_of=run["id"],
        )
        self.database.enqueue_outbox(
            int(run["telegram_chat_id"]),
            f"🔁 Investigation {run['id'][:8]} omitted "
            f"{', '.join(missing)}; continuation {continuation['id'][:8]} "
            "queued.",
            kind="investigation",
            run_id=continuation["id"],
        )

    async def _watch_cancel(
        self, run_id: str, process: asyncio.subprocess.Process
    ) -> None:
        while process.returncode is None:
            await asyncio.sleep(2)
            if self.database.cancel_requested(run_id):
                await self._terminate(process)
                return

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        await self._signal_group(process.pid, "TERM")
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
            return
        except TimeoutError:
            pass
        await self._signal_group(process.pid, "KILL")
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            log.error("Process group %s survived SIGKILL", process.pid)

    async def _signal_group(self, process_group_id: int, name: str) -> None:
        try:
            os.killpg(process_group_id, getattr(signal, f"SIG{name}"))
        except (ProcessLookupError, PermissionError):
            pass
        try:
            killer = await asyncio.create_subprocess_exec(
                "/usr/bin/sudo",
                "-n",
                "-H",
                "-u",
                "receptionist-agent",
                self.config.agent_killer,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            log.warning("Could not launch agent killer: %s", error)
            return
        await killer.communicate(
            json.dumps(
                {"process_group_id": process_group_id, "signal": name}
            ).encode()
        )

    @staticmethod
    async def _read_stderr(process: asyncio.subprocess.Process) -> str:
        assert process.stderr is not None
        data = await process.stderr.read()
        return data.decode(errors="replace")

    def _notify(
        self,
        body: str,
        *,
        kind: str = "deployment",
        reply_markup: list[list[dict[str, str]]] | None = None,
    ) -> None:
        chat_id = self.database.primary_chat_id() or self.config.allowed_user_id
        if not chat_id:
            log.warning("No Telegram chat for notification: %s", body[:200])
            return
        self.database.enqueue_outbox(
            chat_id, body, kind=kind, reply_markup=reply_markup
        )

    def release_revision(self) -> str:
        try:
            return self.config.release_link.resolve(strict=True).name
        except OSError:
            return "unknown"

    def finish_self_deployment(self) -> None:
        drain = self.database.get_state("deploy_drain", "")
        if not drain:
            return
        request_id = drain.split("@", 1)[0]
        revision = self.release_revision()
        try:
            request = self.database.get_deployment_request(request_id)
        except LookupError:
            request = None
        if request and request["status"] == "running":
            if request["revision"] == revision:
                self.database.finish_deployment_request(
                    request_id,
                    status="succeeded",
                    exit_code=0,
                    output=(
                        "Worker restarted at requested revision "
                        f"{revision}."
                    ),
                    error=None,
                )
            else:
                self.database.finish_deployment_request(
                    request_id,
                    status="failed",
                    exit_code=None,
                    output=None,
                    error=(
                        "Worker restarted at revision "
                        f"{revision}, not requested revision "
                        f"{request['revision']}."
                    ),
                )
        self.database.set_state("deploy_drain", "")
        self._notify(
            f"🚀 Receptionist2 deployment {request_id[:8]} "
            f"finished. Worker restarted at revision "
            f"{revision[:12]}; queued prompts resume now."
        )

    async def _deployment_loop(self) -> None:
        while True:
            try:
                await self.poll_deployments()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Deployment poll failed")
            await asyncio.sleep(5)

    def ingest_deployment_requests(self) -> None:
        request_dir = self.config.deploy_request_dir
        if not request_dir.is_dir():
            return
        user_id = self.config.allowed_user_id or (
            self.database.primary_user_id() or 0
        )
        for path in sorted(request_dir.glob("*.json"))[:20]:
            try:
                parsed = parse_request_file(path, self.config.repo_root)
                self.database.import_deployment_request(
                    user_id=user_id, **parsed
                )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ):
                log.exception("Rejected deployment request file %s", path)
            finally:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    log.exception("Could not remove request file %s", path)

    async def poll_deployments(self) -> None:
        self.ingest_deployment_requests()
        self.database.expire_deployment_requests()
        self._expire_stale_deploy_drain()
        for request in self.database.pending_deployment_requests():
            if not self.database.transition_deployment_request(
                request["id"], "pending", "approved"
            ):
                continue
            request = self.database.get_deployment_request(request["id"])
            self._notify(started_text(request))
            task = asyncio.create_task(self.execute_deployment(request))
            self._deployment_tasks.add(task)
            task.add_done_callback(self._deployment_tasks.discard)

    def _expire_stale_deploy_drain(self) -> None:
        value = self.database.get_state("deploy_drain", "")
        if not value or self._deployment_lock.locked():
            return
        _, _, started = value.partition("@")
        try:
            age = time.time() - float(started)
        except ValueError:
            age = DEPLOY_DRAIN_STALE_SECONDS
        if age < DEPLOY_DRAIN_STALE_SECONDS:
            return
        self.database.set_state("deploy_drain", "")
        self._notify(
            "⚠️ Receptionist2 self-deployment did not restart the worker "
            "within 20 minutes. Queued prompts are resuming on the current "
            f"release {self.release_revision()[:12]}."
        )

    async def execute_deployment(self, request: dict[str, Any]) -> None:
        self_deployment = is_self_deployment(request["command"])
        async with self._deployment_lock:
            if self_deployment:
                self.database.set_state(
                    "deploy_drain", f"{request['id']}@{time.time()}"
                )
                while self.database.running_runs():
                    await asyncio.sleep(2)
            if not self.database.transition_deployment_request(
                request["id"], "approved", "running"
            ):
                if self_deployment:
                    self.database.set_state("deploy_drain", "")
                return
            payload = dict(request)
            payload["status"] = "approved"
            exit_code: int | None = None
            output = ""
            error: str | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/sudo",
                    "-n",
                    self.config.deploy_executor,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as launch_error:
                process = None
                error = f"Could not start deployment executor: {launch_error}"
            if process is not None:
                try:
                    stdout, _ = await asyncio.wait_for(
                        process.communicate(
                            json.dumps(payload, ensure_ascii=False).encode()
                        ),
                        timeout=self.config.deploy_timeout_seconds,
                    )
                    output = stdout.decode(errors="replace").strip()
                    exit_code = process.returncode
                    if exit_code:
                        error = f"Executor exited with status {exit_code}."
                except TimeoutError:
                    process.terminate()
                    await process.wait()
                    exit_code = process.returncode
                    error = (
                        "Deployment exceeded "
                        f"{self.config.deploy_timeout_seconds} seconds."
                    )
            status = "failed" if error else "succeeded"
            self.database.finish_deployment_request(
                request["id"],
                status=status,
                exit_code=exit_code,
                output=output[-8000:] or None,
                error=error,
            )
            if status == "failed" and self_deployment:
                self.database.set_state("deploy_drain", "")
            if status == "succeeded" and self_deployment:
                message = (
                    f"⏳ Receptionist2 deployment {request['id'][:8]} "
                    "launched. New prompts stay queued until the worker "
                    "restarts on the new release."
                )
            else:
                message = (
                    f"{'✅' if status == 'succeeded' else '❌'} Deployment "
                    f"{request['id'][:8]} {status}."
                )
            if error:
                message += f"\n{error}"
            if output:
                message += f"\n\nLast output:\n{output[-2500:]}"
            self._notify(
                message,
                reply_markup=(
                    diagnose_markup(request["id"])
                    if status == "failed"
                    else None
                ),
            )


def main() -> None:
    config = WorkerConfig.from_env()
    worker = Worker(config, Database(config.database_path))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop)
    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
