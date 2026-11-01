"""Runtime configuration, loaded from environment variables (and `.env` locally).

In AWS the same variables are injected into the ECS task from Secrets Manager.
The private key can be supplied either as a file path (local dev) or inline (ECS).
"""

import ssl
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Only what database tooling (migrations, reports) needs — no GitHub config required."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: SecretStr


class Settings(DatabaseSettings):
    github_org: str
    github_app_id: int
    github_installation_id: int
    github_private_key_path: Path | None = None
    github_private_key: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    # Pinned REST API version (supported list: `gh api /versions`).
    github_api_version: str = "2022-11-28"
    # Extra CA certificates (PEM) to trust in addition to the public ones, e.g. the corporate
    # SSL-inspection root. Leave unset where traffic isn't inspected.
    ca_bundle: Path | None = None

    collector_concurrency: int = Field(default=8, ge=1, le=32)
    # Pause API calls when the remaining rate-limit budget drops below this.
    rate_limit_floor: int = Field(default=500, ge=0)

    @model_validator(mode="after")
    def _require_private_key(self) -> Settings:
        if self.github_private_key is not None:
            return self
        if self.github_private_key_path is None:
            raise ValueError("Set GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH")
        if not self.github_private_key_path.is_file():
            raise ValueError(f"Private key file not found: {self.github_private_key_path}")
        return self

    @model_validator(mode="after")
    def _check_ca_bundle(self) -> Settings:
        """Fail at startup (not mid-sync) if CA_BUNDLE is missing or not PEM certificates."""
        if self.ca_bundle is None:
            return self
        if not self.ca_bundle.is_file():
            raise ValueError(f"CA bundle file not found: {self.ca_bundle}")
        try:
            ssl.create_default_context().load_verify_locations(cafile=str(self.ca_bundle))
        except ssl.SSLError as exc:
            raise ValueError(f"CA bundle is not valid PEM certificates: {self.ca_bundle}") from exc
        return self

    def private_key_pem(self) -> str:
        if self.github_private_key is not None:
            return self.github_private_key.get_secret_value()
        assert self.github_private_key_path is not None
        return self.github_private_key_path.read_text(encoding="utf-8")


@lru_cache
def get_db_settings() -> DatabaseSettings:
    return DatabaseSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment / .env
