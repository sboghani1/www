import os
import stat
import sys
import textwrap

import pytest

from receptionist2 import claude_token
from receptionist2.claude_token import (
    find_auth_url,
    find_token,
    prompt_ready,
    store_token,
)

TOKEN = "sk-ant-oat01-" + "A1b2_c3-" * 12 + "AA"
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"


def test_prompt_and_url_survive_terminal_escapes() -> None:
    # Shaped like `claude setup-token` output: words are positioned with
    # cursor moves and the URL is an OSC 8 hyperlink.
    raw = (
        "\x1b7\x1b8Browser\x1b[9Gdidn't\x1b[16Gopen?\n"
        f"\x1b]8;id=cy2slw;{URL}\x1b\\{URL[:40]}\x1b]8;;\x1b\\\n"
    )
    assert find_auth_url(raw) == URL
    assert not prompt_ready(raw)
    raw += "Paste\x1b[7Gcode\x1b[12Ghere\x1b[17Gif\x1b[20Gprompted\x1b[29G>"
    assert prompt_ready(raw)


def test_find_token_waits_for_the_whole_token() -> None:
    assert find_token("Your token:\n" + TOKEN[:40]) is None
    assert find_token("Your token:\n" + TOKEN + "\n\nStore it") == TOKEN
    assert find_token("\x1b[1m" + TOKEN + "\x1b[22m") == TOKEN


def test_store_token_keeps_other_lines_and_backs_up(tmp_path) -> None:
    path = tmp_path / ".claude" / "auth.env"
    path.parent.mkdir()
    previous = "# keep me\nOTHER=1\nexport CLAUDE_CODE_OAUTH_TOKEN=old\n"
    path.write_text(previous)

    store_token(TOKEN, path)

    assert path.read_text() == (
        f"# keep me\nOTHER=1\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"
    )
    backup = path.with_name("auth.env.bak")
    assert backup.read_text() == previous
    for written in (path, backup):
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert sorted(item.name for item in path.parent.iterdir()) == [
        "auth.env",
        "auth.env.bak",
    ]


def test_store_token_rejects_unexpected_format(tmp_path) -> None:
    path = tmp_path / "auth.env"
    with pytest.raises(ValueError):
        store_token("not-a-token", path)
    assert not path.exists()
    store_token(TOKEN, path)
    assert path.read_text() == f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"
    assert not path.with_name("auth.env.bak").exists()


def fake_claude(tmp_path, after_code: str) -> str:
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import sys, time
            sys.stdout.write("Browser\\x1b[9Gdidn't open?\\n")
            sys.stdout.write("\\x1b]8;id=x;{URL}\\x07link\\x1b]8;;\\x07\\n")
            sys.stdout.write("Paste\\x1b[7Gcode\\x1b[12Ghere\\x1b[17Gif >")
            sys.stdout.flush()
            code = sys.stdin.readline().strip()
            {after_code}
            """
        )
    )
    script.chmod(0o755)
    return str(script)


def feed_stdin(monkeypatch, text: str) -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, text.encode())
    os.close(write_end)
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end))


def test_run_relays_url_and_stores_token_without_printing_it(
    tmp_path, monkeypatch, capsys
) -> None:
    binary = fake_claude(
        tmp_path,
        f"""
            if code != "the-code#state":
                print("Invalid code")
                sys.exit(1)
            print("\\nToken:\\n{TOKEN}\\n\\nStore this token securely.")
            sys.stdout.flush()
            time.sleep(30)
        """,
    )
    auth_env = tmp_path / "auth.env"
    monkeypatch.setattr(claude_token, "CLAUDE_BINARY", binary)
    monkeypatch.setattr(claude_token, "AUTH_ENV", auth_env)
    feed_stdin(monkeypatch, "the-code#state\n")

    assert claude_token.run() == 0

    output = capsys.readouterr().out
    assert output.splitlines() == [
        URL,
        "Paste code here",
        "Claude long-lived token saved.",
    ]
    assert "sk-ant-" not in output
    assert auth_env.read_text() == f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"


def test_run_reports_failure_and_keeps_auth_env(
    tmp_path, monkeypatch, capsys
) -> None:
    binary = fake_claude(
        tmp_path,
        """
            print("OAuth error: invalid code")
            sys.exit(1)
        """,
    )
    auth_env = tmp_path / "auth.env"
    auth_env.write_text("CLAUDE_CODE_OAUTH_TOKEN=existing\n")
    monkeypatch.setattr(claude_token, "CLAUDE_BINARY", binary)
    monkeypatch.setattr(claude_token, "AUTH_ENV", auth_env)
    feed_stdin(monkeypatch, "wrong\n")

    assert claude_token.run() == 1

    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith("Claude did not issue a token:")
    assert "invalid code" in last
    assert auth_env.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=existing\n"
