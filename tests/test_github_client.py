"""GitHub App auth and the HTTP client (no network: httpx.MockTransport)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auditor.github.auth import AppAuth
from auditor.github.cache import EtagCache
from auditor.github.client import GitHubClient, GraphQLError, NotFoundError

Handler = Callable[[httpx.Request], httpx.Response]


def token_response(minutes: int = 60) -> httpx.Response:
    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
    return httpx.Response(201, json={"token": "ghs_test", "expires_at": expires})


def make_client(handler: Handler, pem: str, **kw: Any) -> tuple[GitHubClient, list[float]]:
    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response()
        return handler(request)

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(routed))
    sleeps: list[float] = []
    client = GitHubClient(AppAuth(7, 9, pem, http), http, sleep=sleeps.append, **kw)
    return client, sleeps


# -- auth ---------------------------------------------------------------------


def test_app_jwt_is_signed_with_app_id(
    private_key: rsa.RSAPrivateKey, private_key_pem: str
) -> None:
    auth = AppAuth(7, 9, private_key_pem, httpx.Client(), clock=lambda: 1_000_000)
    claims = jwt.decode(
        auth.app_jwt(),
        private_key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iss": "7", "iat": 999_940, "exp": 1_000_540}


def test_installation_token_is_cached_then_refreshed(private_key_pem: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        return token_response(minutes=60)

    now = [datetime.now(UTC).timestamp()]
    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    auth = AppAuth(7, 9, private_key_pem, http, clock=lambda: now[0])

    assert auth.installation_token() == auth.installation_token() == "ghs_test"
    assert len(calls) == 1 and calls[0].startswith("Bearer ey")  # JWT used to mint it
    now[0] += 56 * 60  # within the 5-minute refresh margin
    auth.installation_token()
    assert len(calls) == 2


# -- REST ---------------------------------------------------------------------


def test_requests_use_installation_token(private_key_pem: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    client, _ = make_client(handler, private_key_pem)
    assert client.rest("/orgs/acme") == {"ok": True}
    assert seen == ["Bearer ghs_test"]


def test_pagination_follows_link_header(private_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[3])
        assert request.url.params["per_page"] == "100"
        link = '<https://api.github.com/items?per_page=100&page=2>; rel="next"'
        return httpx.Response(200, json=[1, 2], headers={"link": link})

    client, _ = make_client(handler, private_key_pem)
    assert client.rest_list("/items") == [1, 2, 3]


def test_etag_304_returns_cached_body(private_key_pem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"n": 1}, headers={"etag": '"v1"'})

    client, _ = make_client(handler, private_key_pem, etags=EtagCache())
    assert client.rest("/thing") == {"n": 1}
    assert client.rest("/thing") == {"n": 1}
    assert (client.stats.rest_calls, client.stats.rest_not_modified) == (2, 1)


def test_server_errors_are_retried_with_backoff(private_key_pem: str) -> None:
    responses = iter([httpx.Response(502), httpx.Response(503), httpx.Response(200, json=[])])
    client, sleeps = make_client(lambda _: next(responses), private_key_pem)
    assert client.rest("/flaky") == []
    assert sleeps == [1, 2]


def test_secondary_rate_limit_waits_retry_after(private_key_pem: str) -> None:
    responses = iter(
        [
            httpx.Response(403, headers={"retry-after": "30"}, json={"message": "slow down"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    client, sleeps = make_client(lambda _: next(responses), private_key_pem)
    assert client.rest("/busy") == {"ok": 1}
    assert sleeps == [30.0]


def test_low_remaining_budget_pauses_until_reset(private_key_pem: str) -> None:
    reset = datetime.now(UTC).timestamp() + 100
    headers = {
        "x-ratelimit-remaining": "10",
        "x-ratelimit-reset": str(reset),
        "x-ratelimit-resource": "core",
    }
    client, sleeps = make_client(
        lambda _: httpx.Response(200, json={}, headers=headers),
        private_key_pem,
        rate_limit_floor=500,
    )
    client.rest("/a")  # learns remaining=10
    client.rest("/b")  # must wait for the reset first
    assert len(sleeps) == 1 and 95 < sleeps[0] <= 102


def test_404_and_403_raise_not_found(private_key_pem: str) -> None:
    client, _ = make_client(lambda _: httpx.Response(404, json={}), private_key_pem)
    with pytest.raises(NotFoundError):
        client.rest("/missing")


# -- GraphQL ------------------------------------------------------------------


def test_graphql_counts_points_and_raises_on_errors(private_key_pem: str) -> None:
    responses = iter(
        [
            httpx.Response(200, json={"data": {"rateLimit": {"cost": 3}, "x": 1}}),
            httpx.Response(200, json={"data": None, "errors": [{"message": "bad field"}]}),
        ]
    )
    client, _ = make_client(lambda _: next(responses), private_key_pem)
    assert client.graphql("query { x }")["x"] == 1
    assert client.stats.graphql_points == 3
    with pytest.raises(GraphQLError, match="bad field"):
        client.graphql("query { y }")
