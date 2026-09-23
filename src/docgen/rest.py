from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from docgen.errors import TalosError

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_RETRY_STATUSES = frozenset({429, 500, 502, 503})
_RETRY_ATTEMPTS = 4


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


def _resolved_quota_project(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    import os

    for name in ("GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        value = os.environ.get(name)
        if value:
            return value
    from docgen.env import load_terraform_output

    project = load_terraform_output().get("GOOGLE_CLOUD_PROJECT")
    return project or None


class RequestsRest:
    def __init__(
        self, credentials: Any | None = None, *, quota_project: str | None = None
    ) -> None:
        self._credentials = credentials
        self._quota_project = quota_project

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        timeout: float = 60.0,
    ) -> RestResponse:
        credentials = self._ready_credentials()
        headers = {"Content-Type": "application/json"}
        apply = getattr(credentials, "apply", None)
        if callable(apply):
            apply(headers)
        else:
            headers["Authorization"] = f"Bearer {credentials.token}"
        # User ADC is rejected by Discovery Engine unless the quota project is set.
        quota = getattr(credentials, "quota_project_id", None) or self._quota_project
        if isinstance(quota, str) and quota and "x-goog-user-project" not in headers:
            headers["x-goog-user-project"] = quota
        last_error: requests.RequestException | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == _RETRY_ATTEMPTS - 1:
                    break
                time.sleep(float(2**attempt))
                continue
            if (
                response.status_code not in _RETRY_STATUSES
                or attempt == _RETRY_ATTEMPTS - 1
            ):
                return _rest_response(response)
            time.sleep(float(2**attempt))
        raise TalosError(f"request failed: {last_error}", exit_code=1) from last_error

    def _ready_credentials(self) -> Any:
        if self._credentials is None:
            import google.auth

            quota = _resolved_quota_project(self._quota_project)
            credentials, detected = google.auth.default(
                scopes=[CLOUD_PLATFORM_SCOPE],
                quota_project_id=quota,
            )
            if not getattr(credentials, "quota_project_id", None):
                fallback = quota or detected
                with_quota = getattr(credentials, "with_quota_project", None)
                if fallback and callable(with_quota):
                    credentials = with_quota(fallback)
            self._credentials = credentials
            if not self._quota_project:
                resolved = getattr(credentials, "quota_project_id", None) or detected
                if isinstance(resolved, str) and resolved:
                    self._quota_project = resolved
        credentials = self._credentials
        if not getattr(credentials, "valid", False):
            import google.auth.transport.requests

            credentials.refresh(google.auth.transport.requests.Request())
        token = getattr(credentials, "token", None)
        if not isinstance(token, str) or not token:
            raise TalosError(
                "Google credentials did not yield an access token", exit_code=2
            )
        return credentials


def _rest_response(response: requests.Response) -> RestResponse:
    body: Any = None
    text = response.text or ""
    if text:
        try:
            body = response.json()
        except ValueError:
            body = None
    return RestResponse(status_code=response.status_code, json=body, text=text)


def raise_for_status(response: RestResponse, action: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip() or str(response.json)
    if len(detail) > 500:
        detail = detail[:500] + "…"
    raise TalosError(
        f"{action} failed: HTTP {response.status_code} {detail}", exit_code=1
    )
