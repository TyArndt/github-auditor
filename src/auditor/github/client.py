"""HTTP client for the GitHub REST and GraphQL APIs.

Handles everything that makes crawling a large organization safe:
- installation-token auth (auto-refreshed)
- retries with backoff on network errors and 5xx
- primary rate limit: pauses when remaining budget drops below a floor
- secondary rate limits (403/429 + Retry-After): waits as instructed
- ETag conditional requests: a 304 Not Modified is free (not counted by GitHub)
- REST pagination via the Link header
Thread-safe: one client is shared by all collector worker threads.
"""

import re
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import certifi
import httpx

from auditor.github.auth import AppAuth
from auditor.github.cache import EtagCache

DEFAULT_API_VERSION = "2022-11-28"
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
_MAX_WAIT_SECONDS = 3600


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFoundError(GitHubError):
    """404, or 403 for a feature the App can't see (treated as 'not available')."""


class GraphQLError(GitHubError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(str(e.get("message")) for e in errors))
        self.errors = errors


@dataclass
class ApiStats:
    rest_calls: int = 0
    rest_not_modified: int = 0
    graphql_calls: int = 0
    graphql_points: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)


def ssl_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """Trust the public CAs (certifi) PLUS an optional extra bundle.

    The extra bundle is for corporate SSL inspection: inspected traffic is re-signed by the
    company's root CA, while hosts excluded from inspection still present public certificates,
    so both must be trusted. Verification is never disabled.
    """
    context = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle is not None:
        context.load_verify_locations(cafile=str(ca_bundle))
    return context


def make_http(
    api_url: str,
    api_version: str = DEFAULT_API_VERSION,
    ca_bundle: Path | None = None,
    **kwargs: Any,
) -> httpx.Client:
    return httpx.Client(
        base_url=api_url,
        verify=ssl_context(ca_bundle),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": api_version,
            "User-Agent": "github-auditor",
        },
        timeout=httpx.Timeout(30.0, connect=10.0),
        **kwargs,
    )


class GitHubClient:
    def __init__(
        self,
        auth: AppAuth,
        http: httpx.Client,
        etags: EtagCache | None = None,
        rate_limit_floor: int = 500,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.auth = auth
        self.http = http
        self.etags = etags
        self.rate_limit_floor = rate_limit_floor
        self.max_retries = max_retries
        self.stats = ApiStats()
        self._sleep = sleep
        self._clock = clock
        self._limits: dict[str, tuple[int, float]] = {}  # resource -> (remaining, reset_epoch)
        self._limits_lock = threading.Lock()

    # -- REST -----------------------------------------------------------------

    def rest(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET one resource (or one page). Returns the decoded JSON body."""
        body, _ = self._get(path, params)
        return body

    def rest_pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Yield each page body, following Link: rel="next" (per_page defaults to 100)."""
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        while url:
            body, url = self._get(url, query)
            query = None  # the next-link URL already carries the query string
            yield body

    def rest_list(
        self, path: str, params: dict[str, Any] | None = None, key: str | None = None
    ) -> list[Any]:
        """All items across pages. `key` unwraps responses like {"workflows": [...]}."""
        items: list[Any] = []
        for page in self.rest_pages(path, params):
            items.extend(page[key] if key else page)
        return items

    def _get(self, url: str, params: dict[str, Any] | None) -> tuple[Any, str | None]:
        cache_key = str(self.http.build_request("GET", url, params=params).url)
        cached = self.etags.get(cache_key) if self.etags else None
        headers = {"If-None-Match": cached.etag} if cached else {}
        response = self._send("GET", url, params=params, headers=headers)

        if response.status_code == 304 and cached:
            self.stats.add(rest_calls=1, rest_not_modified=1)
            return cached.body, cached.next_url
        self.stats.add(rest_calls=1)
        self._raise_for_status(response, url)

        body = response.json()
        match = _NEXT_LINK.search(response.headers.get("link", ""))
        next_url = match.group(1) if match else None
        etag = response.headers.get("etag")
        if self.etags is not None and etag:
            self.etags.put(cache_key, etag, body, next_url)
        return body, next_url

    # -- GraphQL --------------------------------------------------------------

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._send("POST", "/graphql", json={"query": query, "variables": variables})
        self._raise_for_status(response, "/graphql")
        payload = response.json()
        if payload.get("errors"):
            raise GraphQLError(payload["errors"])
        data: dict[str, Any] = payload["data"]
        cost = (data.get("rateLimit") or {}).get("cost", 1)
        self.stats.add(graphql_calls=1, graphql_points=int(cost))
        return data

    # -- transport ------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {}) or {}
        for attempt in range(self.max_retries + 1):
            self._respect_rate_floor()
            headers = {"Authorization": f"Bearer {self.auth.installation_token()}", **extra_headers}
            try:
                response = self.http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt)
                continue

            self._record_limits(response)
            if response.status_code >= 500 and attempt < self.max_retries:
                self._backoff(attempt)
                continue
            if self._is_rate_limited(response) and attempt < self.max_retries:
                self.stats.add(rate_limit_waits=1)
                self._sleep(self._rate_limit_wait(response))
                continue
            return response
        raise GitHubError(f"{method} {url}: retries exhausted")  # pragma: no cover

    def _backoff(self, attempt: int) -> None:
        self.stats.add(retries=1)
        self._sleep(min(2**attempt, 60))

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        return (
            "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in response.text.lower()
        )

    def _rate_limit_wait(self, response: httpx.Response) -> float:
        if retry_after := response.headers.get("retry-after"):
            return min(float(retry_after), _MAX_WAIT_SECONDS)
        if reset := response.headers.get("x-ratelimit-reset"):
            return min(max(float(reset) - self._clock() + 1, 1), _MAX_WAIT_SECONDS)
        return 60.0  # secondary limit without headers: GitHub asks for at least a minute

    def _record_limits(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        resource = response.headers.get("x-ratelimit-resource", "core")
        with self._limits_lock:
            self._limits[resource] = (int(remaining), float(reset))

    def _respect_rate_floor(self) -> None:
        with self._limits_lock:
            waits = [
                reset - self._clock() + 1
                for remaining, reset in self._limits.values()
                if remaining < self.rate_limit_floor and reset > self._clock()
            ]
        if waits:
            self.stats.add(rate_limit_waits=1)
            self._sleep(min(max(waits), _MAX_WAIT_SECONDS))
            with self._limits_lock:
                self._limits.clear()

    @staticmethod
    def _raise_for_status(response: httpx.Response, url: str) -> None:
        if response.is_success:
            return
        message = (
            f"{response.request.method} {url} -> {response.status_code}: {response.text[:300]}"
        )
        if response.status_code in (403, 404):
            raise NotFoundError(message, response.status_code)
        raise GitHubError(message, response.status_code)

    def rate_limits(self) -> dict[str, Any]:
        """Current limits per resource (GET /rate_limit is free)."""
        resources: dict[str, Any] = self.rest("/rate_limit")["resources"]
        return resources
