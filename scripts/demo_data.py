"""Load demo data (the test suite's fake GitHub org) into the local database.

For building dashboards without a GitHub App. Runs a real `run_sync` against the
in-memory fake, so the data goes through the same collector and loader as production.

    python scripts/demo_data.py            # uses DATABASE_URL from .env / environment

Don't point this at a database that holds real data: the demo org is merged into it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `tests`

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auditor.config import Settings, get_db_settings
from auditor.github.auth import AppAuth
from auditor.github.cache import EtagCache
from auditor.github.client import GitHubClient
from auditor.sync import run_sync
from tests.fake_github import FakeGitHub


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    settings = Settings(
        _env_file=None,
        github_org="acme",
        github_app_id=1,
        github_installation_id=1,
        github_private_key=pem,
        database_url=get_db_settings().database_url,
    )
    fake = FakeGitHub()

    def client_factory(etags: EtagCache) -> GitHubClient:
        http = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler)
        )
        return GitHubClient(AppAuth(1, 1, pem, http), http, etags, sleep=lambda _: None)

    result = run_sync(settings, client_factory=client_factory)
    print(
        f"demo org 'acme' loaded: run {result.run_id} {result.status}, "
        f"{result.repos_seen} repos, {result.branches_seen} branches"
    )


if __name__ == "__main__":
    main()
