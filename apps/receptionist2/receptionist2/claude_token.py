"""Create Claude's long-lived subscription token and store it in auth.env.

Runs inside the transient unit started by ``receptionist2-control reauth``,
as receptionist-agent and outside the watchdog sandbox. ``claude
setup-token`` only renders in a terminal, so it runs in a pseudo-terminal
here. The authorization URL is relayed to the watchdog as plain text, the
one-time code is read from stdin, and the resulting token is written straight
into ~/.claude/auth.env. The token itself is never printed.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

CLAUDE_BINARY = "/usr/bin/claude"
AUTH_ENV = Path("/home/receptionist-agent/.claude/auth.env")
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,}")
SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
AUTH_URL_RE = re.compile(r"https://claude\.com/[^\s\x07\x1b]+")
CURSOR_MOVE_RE = re.compile(r"\x1b\[\d*[CG]")
ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[()*+][0-~]"
    r"|\x1b[0-~]"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROMPT_MARKER = "pastecodehere"
PROMPT_TIMEOUT_SECONDS = 45
CODE_TIMEOUT_SECONDS = 330
TOKEN_TIMEOUT_SECONDS = 75
# Wide enough that neither the URL nor the token is ever wrapped.
TERMINAL_COLUMNS = 1000
TERMINAL_ROWS = 50


def visible_text(raw: str) -> str:
    # The TUI positions words with cursor moves instead of spaces.
    text = CURSOR_MOVE_RE.sub(" ", raw)
    text = ESCAPE_RE.sub("", text)
    return CONTROL_RE.sub("", text)


def prompt_ready(raw: str) -> bool:
    compact = re.sub(r"\s+", "", visible_text(raw)).lower()
    return PROMPT_MARKER in compact


def find_auth_url(raw: str) -> str | None:
    match = AUTH_URL_RE.search(raw)
    return match.group(0) if match else None


def find_token(raw: str) -> str | None:
    for text in (raw, visible_text(raw)):
        for match in TOKEN_RE.finditer(text):
            # A match touching the end of the buffer may still be arriving.
            if match.end() < len(text):
                return match.group(0)
    return None


def redact(text: str) -> str:
    return SECRET_RE.sub("sk-ant-<redacted>", text)


def summary(raw: str) -> str:
    """One redacted line of the terminal's tail, for failure reports."""
    return " ".join(redact(visible_text(raw)).split())[-300:]


def is_token_line(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, separator, _ = stripped.partition("=")
    return bool(separator) and key.strip() == TOKEN_KEY


def store_token(token: str, path: Path | None = None) -> None:
    """Atomically set the token in auth.env, keeping every other line."""
    path = path or AUTH_ENV
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Claude returned a token in an unexpected format")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    kept: list[str] = []
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        kept = [line for line in previous.splitlines() if not is_token_line(line)]
        backup = path.with_name(path.name + ".bak")
        _write_private(backup, previous)
    _write_private(path, "\n".join([*kept, f"{TOKEN_KEY}={token}"]) + "\n")


def _write_private(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class TerminalSession:
    def __init__(self) -> None:
        master, slave = os.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0),
        )
        environment = dict(os.environ, TERM="xterm-256color")
        self.process = subprocess.Popen(
            [CLAUDE_BINARY, "setup-token"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
            start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
        )
        os.close(slave)
        self.master = master
        self.output = ""

    def read(self, timeout: float) -> bool:
        """Read available output. Returns False once the terminal closed."""
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return True
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return False
        if not chunk:
            return False
        self.output += chunk.decode("utf-8", errors="replace")
        return True

    def type(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))
        time.sleep(0.5)
        os.write(self.master, b"\r")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)


def read_code(session: TerminalSession) -> str:
    deadline = time.monotonic() + CODE_TIMEOUT_SECONDS
    stdin = sys.stdin.fileno()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stdin, session.master], [], [], 1)
        if session.master in ready and not session.read(0):
            raise RuntimeError("Claude exited before the code arrived")
        if stdin in ready:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("no authorization code was provided")
            code = line.strip()
            if not code or len(code) > 512 or CONTROL_RE.search(code):
                raise RuntimeError("the authorization code is malformed")
            return code
    raise RuntimeError("timed out waiting for the authorization code")


def run() -> int:
    session = TerminalSession()
    try:
        deadline = time.monotonic() + PROMPT_TIMEOUT_SECONDS
        url = None
        while time.monotonic() < deadline:
            alive = session.read(1)
            url = find_auth_url(session.output)
            if url and prompt_ready(session.output):
                break
            if not alive:
                break
        if not url or not prompt_ready(session.output):
            print(
                "Claude setup-token did not show an authorization URL: "
                + summary(session.output),
                flush=True,
            )
            return 1
        print(url, flush=True)
        print("Paste code here", flush=True)
        code = read_code(session)
        session.output = ""
        session.type(code)
        code = ""
        deadline = time.monotonic() + TOKEN_TIMEOUT_SECONDS
        token = None
        alive = True
        while token is None and alive and time.monotonic() < deadline:
            alive = session.read(1)
            # Once the terminal has closed the output is final, so a token
            # ending the buffer is complete.
            token = find_token(session.output if alive else session.output + "\n")
        if token is None:
            print(
                "Claude did not issue a token: " + summary(session.output),
                flush=True,
            )
            return 1
        store_token(token)
        token = ""
        print("Claude long-lived token saved.", flush=True)
        return 0
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Claude setup-token failed: {redact(str(error))}", flush=True)
        return 1
    finally:
        session.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
