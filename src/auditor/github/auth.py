"""GitHub App authentication.

    private key --signs--> App JWT (10 min) --exchanges for--> installation token (1 hr)

The installation token is what every data call uses. It is cached and refreshed
automatically shortly before it expires. Thread-safe.
"""

import threading
import time
from collections.abc import Callable
from datetime import datetime

import httpx
import jwt

REFRESH_MARGIN_SECONDS = 300  # refresh the token when < 5 minutes remain


class AppAuth:
    def __init__(
        self,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self._private_key = private_key_pem
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def app_jwt(self) -> str:
        """Short-lived JWT proving we are the App (used only to mint installation tokens)."""
        now = int(self._clock())
        # iat is backdated 60s to tolerate clock drift; GitHub caps exp at 10 minutes.
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def installation_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
                response = self._http.post(
                    f"/app/installations/{self.installation_id}/access_tokens",
                    headers={"Authorization": f"Bearer {self.app_jwt()}"},
                )
                response.raise_for_status()
                data = response.json()
                self._token = str(data["token"])
                self._expires_at = datetime.fromisoformat(data["expires_at"]).timestamp()
            return self._token
