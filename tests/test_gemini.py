from __future__ import annotations

from pathlib import Path

import pytest

from docgen.constants import DEFAULT_CHAT_MODEL
from docgen.env import repo_root
from docgen.gemini import (
    aiplatform_root,
    generate_content_url,
    model_publish_location,
)
from docgen.rest import RestResponse

pytestmark = pytest.mark.unit


def test_regional_workload_publishes_model_on_global() -> None:
    assert model_publish_location("us-east1") == "global"
    url = generate_content_url("lab5-gemini-dev1", "us-east1", "gemini-3.8-flash")
    assert url == (
        "https://aiplatform.googleapis.com/v1/projects/lab5-gemini-dev1"
        "/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    )


_EMBEDDING_ID = "text-embedding-" + "005"
_SCOPE_DIRS = ("src", "agents", "tests", "infra", "docs")
_SCOPE_FILES = (
    "README.md",
    "CHANGELOG.md",
    "Makefile",
    "pyproject.toml",
    "changelog",
)
_SKIP_PARTS = {
    ".venv",
    "__pycache__",
    ".git",
    ".ruff_cache",
    ".pytest_cache",
    ".terraform",
}


def test_default_model_ids() -> None:
    assert DEFAULT_CHAT_MODEL == "gemini-3.8-flash"
    source = (repo_root() / "src/docgen/constants.py").read_text(encoding="utf-8")
    assert "EMBEDDING" not in source


def test_embedding_model_id_absent_outside_spec() -> None:
    root = repo_root()
    paths: list[Path] = []
    for name in _SCOPE_DIRS:
        paths.extend(path for path in (root / name).rglob("*") if path.is_file())
    for name in _SCOPE_FILES:
        candidate = root / name
        if candidate.is_file():
            paths.append(candidate)
    hits: list[str] = []
    for path in paths:
        if path.name == "SPEC.md" or any(part in _SKIP_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _EMBEDDING_ID in text:
            hits.append(str(path.relative_to(root)))
    assert hits == []


def test_explicit_publish_locations_keep_their_hosts() -> None:
    roots = {
        "global": "https://aiplatform.googleapis.com/v1",
        "us": "https://aiplatform.us.rep.googleapis.com/v1",
        "eu": "https://aiplatform.eu.rep.googleapis.com/v1",
    }
    for location, root in roots.items():
        assert model_publish_location(location) == location
        assert aiplatform_root(location) == root
        assert generate_content_url("p", location, DEFAULT_CHAT_MODEL) == (
            f"{root}/projects/p/locations/{location}"
            f"/publishers/google/models/{DEFAULT_CHAT_MODEL}:generateContent"
        )


def test_chat_and_application_post_on_global_for_a_regional_workload() -> None:
    from docgen.application import GeminiChatCompleter
    from docgen.chat import ChatConfig, GeminiChatModel

    class RecordingRest:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def request(
            self,
            method: str,
            url: str,
            *,
            json_body: object = None,
            timeout: float = 60.0,
        ) -> RestResponse:
            del method, json_body, timeout
            self.urls.append(url)
            return RestResponse(
                status_code=200,
                json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
                text="{}",
            )

    rest = RecordingRest()
    expected = generate_content_url("lab5-gemini-dev1", "us-east1", DEFAULT_CHAT_MODEL)
    chat = GeminiChatModel(
        rest,
        ChatConfig(
            project="lab5-gemini-dev1",
            location="us-east1",
            corpus_name="projects/lab5-gemini-dev1/locations/us-east1/ragCorpora/1",
        ),
    )
    assert (
        chat.generate(
            contents=[{"role": "user", "parts": [{"text": "q"}]}],
            system="s",
        )
        == "{}"
    )
    completer = GeminiChatCompleter(
        rest, "lab5-gemini-dev1", "us-east1", DEFAULT_CHAT_MODEL
    )
    assert completer.complete(messages=[{"role": "user", "content": "q"}]) == "{}"
    assert rest.urls == [expected, expected]
    assert "/locations/global/" in expected
    assert expected.startswith("https://aiplatform.googleapis.com/v1/")


def test_generate_retries_resource_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from docgen.chat import ChatConfig, GeminiChatModel

    slept: list[float] = []
    monkeypatch.setattr("docgen.chat.time.sleep", lambda seconds: slept.append(seconds))

    class FlakyRest:
        def __init__(self) -> None:
            self.calls = 0

        def request(
            self,
            method: str,
            url: str,
            *,
            json_body: object = None,
            timeout: float = 60.0,
        ) -> RestResponse:
            del method, url, json_body, timeout
            self.calls += 1
            if self.calls < 3:
                return RestResponse(
                    status_code=429,
                    json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
                    text="exhausted",
                )
            return RestResponse(
                status_code=200,
                json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
                text="ok",
            )

    rest = FlakyRest()
    chat = GeminiChatModel(
        rest,
        ChatConfig(
            project="lab5-gemini-dev1",
            location="us-east1",
            corpus_name="projects/p/locations/us-east5/ragCorpora/1",
        ),
    )
    assert (
        chat.generate(
            contents=[{"role": "user", "parts": [{"text": "q"}]}],
            system="s",
        )
        == "ok"
    )
    assert rest.calls == 3
    assert slept == [2.0, 4.0]


def test_workload_region_does_not_remap_to_us_east5() -> None:
    text = (repo_root() / "src/docgen/gemini.py").read_text(encoding="utf-8")
    assert "us-east5" not in text
    assert "ragCorpora" not in text
    assert model_publish_location("us-east1") == "global"
