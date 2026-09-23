from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docgen.errors import TalosError
from docgen.rest import CLOUD_PLATFORM_SCOPE, RequestsRest

pytestmark = pytest.mark.unit


class _Response:
    status_code = 200
    text = "{}"

    def json(self) -> dict[str, Any]:
        return {}


class _Creds:
    def __init__(self, quota_project_id: str | None) -> None:
        self.valid = False
        self.token: str | None = None
        self.quota_project_id = quota_project_id
        self.refreshed = False

    def refresh(self, _request: object) -> None:
        self.refreshed = True
        self.valid = True
        self.token = "ya29.test"

    def apply(self, headers: dict[str, str], token: str | None = None) -> None:
        headers["authorization"] = f"Bearer {token or self.token}"
        if self.quota_project_id:
            headers["x-goog-user-project"] = self.quota_project_id


def _install_auth(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    quota_on_creds: str | None,
) -> None:
    def fake_default(
        scopes: object = None,
        request: object = None,
        quota_project_id: str | None = None,
        default_scopes: object = None,
    ) -> tuple[_Creds, str]:
        captured["scopes"] = scopes
        captured["quota_project_id"] = quota_project_id
        return _Creds(quota_on_creds or quota_project_id), "detected-project"

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json: object = None,
        timeout: float | None = None,
    ) -> _Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("google.auth.default", fake_default)
    monkeypatch.setattr("docgen.rest.requests.request", fake_request)


def test_request_sends_quota_project_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "lab5-gemini-dev1")
    monkeypatch.delenv("GOOGLE_CLOUD_QUOTA_PROJECT", raising=False)
    captured: dict[str, Any] = {}
    _install_auth(monkeypatch, captured, quota_on_creds="lab5-gemini-dev1")

    response = RequestsRest().request(
        "GET", "https://discoveryengine.googleapis.com/v1/stores/x"
    )

    assert response.status_code == 200
    assert captured["quota_project_id"] == "lab5-gemini-dev1"
    assert captured["scopes"] == [CLOUD_PLATFORM_SCOPE]
    headers = captured["headers"]
    assert headers["authorization"] == "Bearer ya29.test"
    assert headers["x-goog-user-project"] == "lab5-gemini-dev1"
    assert headers["Content-Type"] == "application/json"


def test_quota_project_prefers_explicit_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-project")
    captured: dict[str, Any] = {}
    _install_auth(monkeypatch, captured, quota_on_creds=None)

    RequestsRest(quota_project="lab5-gemini-dev1").request("POST", "https://example")

    assert captured["quota_project_id"] == "lab5-gemini-dev1"
    assert captured["headers"]["x-goog-user-project"] == "lab5-gemini-dev1"


def test_quota_project_falls_back_to_terraform_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_QUOTA_PROJECT", raising=False)
    monkeypatch.setattr("docgen.env.repo_root", lambda: tmp_path)
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "outputs.json").write_text(
        json.dumps({"GOOGLE_CLOUD_PROJECT": {"value": "from-outputs"}}),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}
    _install_auth(monkeypatch, captured, quota_on_creds=None)

    RequestsRest().request("GET", "https://example")

    assert captured["quota_project_id"] == "from-outputs"
    assert captured["headers"]["x-goog-user-project"] == "from-outputs"


def test_retries_unavailable_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "lab5-gemini-dev1")
    statuses = [503, 503, 200]
    calls = {"n": 0}
    sleeps: list[float] = []

    class Response:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.text = "{}" if code == 200 else '{"error":{"code":503}}'

        def json(self) -> dict[str, Any]:
            return {}

    def fake_request(*_args: object, **_kwargs: object) -> Response:
        code = statuses[calls["n"]]
        calls["n"] += 1
        return Response(code)

    monkeypatch.setattr("google.auth.default", lambda **_kwargs: (_Creds("p"), "p"))
    monkeypatch.setattr("docgen.rest.requests.request", fake_request)
    monkeypatch.setattr("docgen.rest.time.sleep", sleeps.append)

    response = RequestsRest().request("GET", "https://example")

    assert response.status_code == 200
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_unavailable_returns_last_status_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "lab5-gemini-dev1")
    calls = {"n": 0}
    sleeps: list[float] = []

    class Response:
        status_code = 503
        text = '{"error":{"message":"unavailable"}}'

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "unavailable"}}

    def fake_request(*_args: object, **_kwargs: object) -> Response:
        calls["n"] += 1
        return Response()

    monkeypatch.setattr("google.auth.default", lambda **_kwargs: (_Creds("p"), "p"))
    monkeypatch.setattr("docgen.rest.requests.request", fake_request)
    monkeypatch.setattr("docgen.rest.time.sleep", sleeps.append)

    response = RequestsRest().request("GET", "https://example")

    assert response.status_code == 503
    assert calls["n"] == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_missing_token_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Empty:
        valid = True
        token = None

        def apply(self, headers: dict[str, str], token: str | None = None) -> None:
            headers["authorization"] = "Bearer"

    monkeypatch.setattr("google.auth.default", lambda **_kwargs: (Empty(), None))

    with pytest.raises(TalosError, match="access token") as exc_info:
        RequestsRest().request("GET", "https://example")
    assert exc_info.value.exit_code == 2
