from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests

from docgen.errors import TalosError

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass
class RestResponse:
    status_code: int
    json: Any
    text: str


class RestClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> RestResponse: ...


class RequestsRest:
    def __init__(self, credentials: Any | None = None) -> None:
        self._credentials = credentials

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> RestResponse:
        token = self._token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise TalosError(f"request failed: {exc}", exit_code=1) from exc
        body: Any = None
        text = response.text or ""
        if text:
            try:
                body = response.json()
            except ValueError:
                body = None
        return RestResponse(status_code=response.status_code, json=body, text=text)

    def _token(self) -> str:
        if self._credentials is None:
            import google.auth

            credentials, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            self._credentials = credentials
        credentials = self._credentials
        if not getattr(credentials, "valid", False):
            import google.auth.transport.requests

            credentials.refresh(google.auth.transport.requests.Request())
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise TalosError(
                "Google credentials did not yield an access token", exit_code=2
            )
        return token


def raise_for_status(response: RestResponse, action: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip() or str(response.json)
    if len(detail) > 500:
        detail = detail[:500] + "…"
    raise TalosError(
        f"{action} failed: HTTP {response.status_code} {detail}", exit_code=1
    )
