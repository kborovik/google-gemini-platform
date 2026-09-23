from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from docgen.env import repo_root, resolve_env
from docgen.errors import TalosError
from docgen.google_chat import (
    CLASS_METHOD,
    STREAM_QUERY_TIMEOUT,
    ChatHandlerConfig,
    RestAgentRuntime,
    config_from_env,
    handle_chat_event,
    http_reply,
    model_text,
    serve,
    session_id,
    stream_query_url,
)
from docgen.rest import RestResponse

pytestmark = pytest.mark.unit

SPACE = "spaces/AAA"
THREAD = "spaces/AAA/threads/TTT"
USER = "users/123"


class FakeRuntime:
    def __init__(self, answer: str = "Max LTV is 80%.") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        self.error: TalosError | None = None

    def stream_query(self, *, user_id: str, session_id: str, message: str) -> str:
        self.calls.append((user_id, session_id, message))
        if self.error is not None:
            raise self.error
        return self.answer


class FakeRest:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.calls: list[tuple[str, str, object, float]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None = None,
        timeout: float = 60.0,
    ) -> RestResponse:
        self.calls.append((method, url, json_body, timeout))
        return RestResponse(status_code=self.status_code, json=None, text=self.text)


def _event(
    *,
    event_type: str = "MESSAGE",
    text: str = "@Officer What is max LTV?",
    argument: str = " What is max LTV?",
    user: str = USER,
    space: str = SPACE,
    thread: str = THREAD,
    sender_type: str = "HUMAN",
) -> dict[str, object]:
    return {
        "type": event_type,
        "user": {"name": user, "type": "HUMAN"},
        "space": {"name": space},
        "message": {
            "text": text,
            "argumentText": argument,
            "sender": {"name": user, "type": sender_type},
            "space": {"name": space},
            "thread": {"name": thread},
        },
    }


def test_v4_message_calls_stream_query_and_replies_in_thread() -> None:
    runtime = FakeRuntime("Max LTV is 80% (CP-RML-2026-01).")
    reply = handle_chat_event(_event(), runtime)
    assert runtime.calls == [
        (USER, session_id(SPACE, THREAD), "What is max LTV?"),
    ]
    assert session_id(SPACE, THREAD) == f"{SPACE} {THREAD}"
    assert reply == {
        "text": "Max LTV is 80% (CP-RML-2026-01).",
        "thread": {"name": THREAD},
    }


def test_other_thread_is_a_different_session() -> None:
    runtime = FakeRuntime("ok")
    other = "spaces/BBB/threads/ZZZ"
    reply = handle_chat_event(
        _event(space="spaces/BBB", thread=other, user="users/9"),
        runtime,
    )
    assert runtime.calls == [
        ("users/9", session_id("spaces/BBB", other), "What is max LTV?")
    ]
    assert reply["thread"] == {"name": other}


def test_non_message_and_bot_do_not_call_runtime() -> None:
    runtime = FakeRuntime()
    assert handle_chat_event(_event(event_type="ADDED_TO_SPACE"), runtime) == {}
    assert handle_chat_event(_event(sender_type="BOT"), runtime) == {}
    assert runtime.calls == []


def test_empty_question_stays_in_thread_without_a_query() -> None:
    runtime = FakeRuntime()
    reply = handle_chat_event(_event(text="   ", argument="  "), runtime)
    assert runtime.calls == []
    assert reply["thread"]["name"] == THREAD
    assert reply["text"] == "Send a question."


def test_stream_query_uses_regional_runtime_and_async_method() -> None:
    url = stream_query_url("lab5-gemini-dev1", "us-east1", "99")
    assert url == (
        "https://us-east1-aiplatform.googleapis.com/v1beta1/"
        "projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/99:streamQuery"
    )
    assert "generateContent" not in url
    assert "us-east5" not in url
    assert "/locations/global/" not in url
    body = {
        "content": {
            "role": "model",
            "parts": [{"text": "Max LTV is 80%."}],
        },
        "author": "InteractiveAgent",
    }
    rest = FakeRest(json.dumps(body))
    runtime = RestAgentRuntime(
        ChatHandlerConfig("lab5-gemini-dev1", "us-east1", "99"),
        client=rest,
    )
    assert runtime.stream_query(user_id=USER, session_id="s t", message="q") == (
        "Max LTV is 80%."
    )
    method, called_url, payload, timeout = rest.calls[0]
    assert method == "POST"
    assert called_url == url
    assert timeout == STREAM_QUERY_TIMEOUT
    assert payload == {
        "classMethod": CLASS_METHOD,
        "input": {"user_id": USER, "session_id": "s t", "message": "q"},
    }
    assert CLASS_METHOD == "async_stream_query"


def test_full_resource_name_sets_the_engine_host() -> None:
    resource = "projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/engine-1"
    url = stream_query_url("other", "europe-west1", resource)
    assert url.startswith("https://us-east1-aiplatform.googleapis.com/")
    assert url.endswith("/reasoningEngines/engine-1:streamQuery")
    assert "europe-west1" not in url


def test_model_text_prefers_final_officer_answer() -> None:
    payload = "\n".join(
        [
            json.dumps(
                {
                    "author": "RetrievalAgent",
                    "content": {"parts": [{"text": "raw policy dump"}]},
                }
            ),
            json.dumps(
                {
                    "author": "InteractiveAgent",
                    "partial": True,
                    "content": {"parts": [{"text": "partial "}]},
                }
            ),
            json.dumps(
                {
                    "author": "InteractiveAgent",
                    "content": {
                        "parts": [
                            {"function_call": {"name": "lookup"}},
                            {"text": "Max LTV is 80%."},
                        ]
                    },
                }
            ),
        ]
    )
    assert model_text(payload) == "Max LTV is 80%."
    partial_only = json.dumps(
        {"partial": True, "content": {"parts": [{"text": "only partial"}]}}
    )
    assert model_text(partial_only) == "only partial"


def test_stream_query_failure_is_posted_in_the_thread() -> None:
    rest = FakeRest("nope", status_code=404)
    runtime = RestAgentRuntime(
        ChatHandlerConfig("lab5-gemini-dev1", "us-east1", "99"),
        client=rest,
    )
    status, reply = http_reply(json.dumps(_event()).encode(), runtime)
    assert status == 200
    assert reply["thread"]["name"] == THREAD
    assert "streamQuery failed" in reply["text"]


def test_invalid_json_does_not_call_runtime() -> None:
    runtime = FakeRuntime()
    status, reply = http_reply(b"{", runtime)
    assert status == 400
    assert reply["text"] == "Invalid JSON."
    assert runtime.calls == []


def test_http_post_replies_in_thread() -> None:
    runtime = FakeRuntime("cited answer")
    server = serve(runtime, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/chat",
            data=json.dumps(_event()).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
            assert response.status == 200
        assert payload == {"text": "cited answer", "thread": {"name": THREAD}}
        assert runtime.calls[0][0] == USER
    finally:
        server.shutdown()
        server.server_close()


def test_missing_reasoning_engine_exits_2() -> None:
    with pytest.raises(TalosError, match="REASONING_ENGINE") as caught:
        config_from_env(
            {
                "GOOGLE_CLOUD_PROJECT": "lab5-gemini-dev1",
                "GOOGLE_CLOUD_LOCATION": "us-east1",
            }
        )
    assert caught.value.exit_code == 2


def test_outputs_supply_reasoning_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "REASONING_ENGINE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("docgen.env.repo_root", lambda: tmp_path)
    infra = tmp_path / "infra"
    infra.mkdir()
    resource = "projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/9"
    (infra / "outputs.json").write_text(
        json.dumps(
            {
                "GOOGLE_CLOUD_PROJECT": {"value": "lab5-gemini-dev1"},
                "GOOGLE_CLOUD_LOCATION": {"value": "us-east1"},
                "REASONING_ENGINE": {"value": resource},
            }
        ),
        encoding="utf-8",
    )
    config = config_from_env(resolve_env(use_terraform=True))
    assert config.project == "lab5-gemini-dev1"
    assert config.location == "us-east1"
    assert config.reasoning_engine == resource


def test_handler_holds_no_policy_text() -> None:
    source = (repo_root() / "src/docgen/google_chat.py").read_text(encoding="utf-8")
    for needle in (
        "credit-policy-agent.instructions",
        "facts.yaml",
        "generateContent",
        "text-embedding-005",
        "Policy ID",
        "That is not in the published policies",
    ):
        assert needle not in source, needle


def test_teams_marker_is_not_hard_skipped() -> None:
    conftest = (repo_root() / "tests/conftest.py").read_text(encoding="utf-8")
    assert "Google Chat is not part of v1" not in conftest
    assert "pytest_runtest_setup" not in conftest
    data = (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    assert "teams: Google Chat live path" in data
    assert "not part of v1" not in data
