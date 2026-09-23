from __future__ import annotations

import pytest

from talos.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PUBLISHER_MODEL,
)
from talos.env import repo_root
from talos.gemini import (
    aiplatform_root,
    generate_content_url,
    model_publish_location,
)
from talos.rest import RestResponse

pytestmark = pytest.mark.unit


def test_regional_workload_publishes_model_on_global() -> None:
    assert model_publish_location("us-east1") == "global"
    url = generate_content_url("lab5-gemini-dev1", "us-east1", "gemini-3.8-flash")
    assert url == (
        "https://aiplatform.googleapis.com/v1/projects/lab5-gemini-dev1"
        "/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    )


def test_default_model_ids() -> None:
    assert DEFAULT_CHAT_MODEL == "gemini-3.8-flash"
    assert DEFAULT_EMBEDDING_MODEL == "text-embedding-005"
    assert (
        DEFAULT_EMBEDDING_PUBLISHER_MODEL
        == "publishers/google/models/text-embedding-005"
    )


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
    from talos.application import GeminiChatCompleter
    from talos.chat import ChatConfig, GeminiChatModel

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
    from talos.chat import ChatConfig, GeminiChatModel

    slept: list[float] = []
    monkeypatch.setattr("talos.chat.time.sleep", lambda seconds: slept.append(seconds))

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
    text = (repo_root() / "src/talos/gemini.py").read_text(encoding="utf-8")
    assert "us-east5" not in text
    assert "ragCorpora" not in text
    assert model_publish_location("us-east1") == "global"
