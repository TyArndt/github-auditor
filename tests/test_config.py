from pathlib import Path

import pytest
from pydantic import ValidationError

from auditor.config import Settings

BASE = {
    "github_org": "acme",
    "github_app_id": 1,
    "github_installation_id": 2,
    "database_url": "postgresql://u:p@localhost/db",
}


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)  # type: ignore[arg-type]


def test_inline_private_key() -> None:
    assert make(github_private_key="PEM").private_key_pem() == "PEM"


def test_private_key_from_file(tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("FILE-PEM", encoding="utf-8")
    assert make(github_private_key_path=key).private_key_pem() == "FILE-PEM"


def test_missing_private_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="GITHUB_PRIVATE_KEY"):
        make()


def test_missing_key_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not found"):
        make(github_private_key_path=tmp_path / "nope.pem")
