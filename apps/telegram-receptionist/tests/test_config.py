from pathlib import Path

import pytest

from receptionist.config import parse_repositories


def test_parse_repositories_stays_under_root(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    repositories = parse_repositories(
        f"www:{root / 'www'},sandbox:{root / 'sandbox'}", root
    )
    assert [repository.name for repository in repositories] == ["www", "sandbox"]


def test_parse_repositories_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    with pytest.raises(ValueError, match="outside"):
        parse_repositories(f"bad:{tmp_path / 'outside'}", root)

