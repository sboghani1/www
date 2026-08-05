import hashlib
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_SHA256 = {
    "001-managing-wnba-odds-polling.md": (
        "173443844af9f7f4e43150d00f4a13a789d7028ea727991ebebccfe439a17c89"
    ),
    "path210.md": (
        "26b1ce8bb89c0d5b204c0e06c7c12ac5fe466fbea9db744a975502e672901bf0"
    ),
    "requirements.txt": (
        "ec3bc02457b478948c8ee781384b47fc4047aef16f4d287b7a3ea05ab27ef6e0"
    ),
    "scratch2": (
        "2ac8f703366dbe11fabd18ccbc1d14a182723563897e8a8fda623dd305fd549f"
    ),
    "scratch3": (
        "762881022f868444f4c09e44c472805231511559fb8509343c81ed71eb2d88c7"
    ),
    "test_wnba_odds_fetch.py": (
        "f2b21946587ff2f89ba1848d0caec7398ac58159cb268f5e7c62a865c26c8e30"
    ),
    "wnba_lines_log.txt": (
        "41b1fa8531c7eabfb1a5abc417099a18da5456126705bf2eb9251168ec7dc951"
    ),
    "wnba_lines.txt": (
        "8031e3d41b7651286b946b26b6d5157fa5ffa0ccbb1b183b025ec57d9ce91e45"
    ),
    "wnba_odds_fetch.py": (
        "4652f886d58d658942fe9cf91965e9382234795aa7a46e3e2ea9fc5ee9bae6ad"
    ),
    "wnba_odds.json": (
        "7786dfb7208c9ddc26231f5621ba00526616a7cb313f778a99cc779bb36ac72b"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_artifacts_are_byte_exact() -> None:
    # Guards the untouched migration evidence under source_artifacts/ only.
    # The *active* apps/wnba-poller/path210.md is intentionally mutated by
    # every published lean (create/revise/delete/undo) -- there used to be
    # a test asserting it stayed byte-identical to this same migration
    # checksum, which only ever held before the first live lean was
    # published. It now fails permanently by design once the system is
    # live, so it was removed rather than asserting a stale invariant.
    artifact_dir = APP_ROOT / "source_artifacts"

    assert {
        path.name for path in artifact_dir.iterdir() if path.is_file()
    } == set(EXPECTED_SHA256)
    for name, expected in EXPECTED_SHA256.items():
        assert _sha256(artifact_dir / name) == expected
