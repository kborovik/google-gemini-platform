from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from docgen.env import repo_root


def _load_chat():
    path = repo_root() / "chat" / "main.py"
    spec = importlib.util.spec_from_file_location("chat_main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chat_main"] = module
    spec.loader.exec_module(module)
    return module


chat_main = _load_chat()
CLASS_METHOD = chat_main.CLASS_METHOD
STREAM_QUERY_TIMEOUT = chat_main.STREAM_QUERY_TIMEOUT
ChatHandlerConfig = chat_main.ChatHandlerConfig
HandlerError = chat_main.HandlerError
RestAgentRuntime = chat_main.RestAgentRuntime
RestResponse = chat_main.RestResponse
ACK_TEXT = chat_main.ACK_TEXT
CHAT_SYSTEM_ACCOUNT = chat_main.CHAT_SYSTEM_ACCOUNT
CHAT_URL = chat_main.CHAT_URL
ENQUEUE_FAILED_TEXT = chat_main.ENQUEUE_FAILED_TEXT
HELP_TEXT = chat_main.HELP_TEXT
OFFICER_ERROR_TEXT = chat_main.OFFICER_ERROR_TEXT
CloudTasksEnqueuer = chat_main.CloudTasksEnqueuer
RestChatPoster = chat_main.RestChatPoster
application_ids = chat_main.application_ids
bind_address = chat_main.bind_address
config_from_env = chat_main.config_from_env
http_judge = chat_main.http_judge
http_reply = chat_main.http_reply
judge_event = chat_main.judge_event
model_text = chat_main.model_text
outcome_request_id = chat_main.outcome_request_id
queue_name = chat_main.queue_name
serve = chat_main.serve
session_id = chat_main.session_id
stream_query_url = chat_main.stream_query_url
task_http_body = chat_main.task_http_body
task_id_from_message = chat_main.task_id_from_message
tasks_invoker_email = chat_main.tasks_invoker_email

pytestmark = pytest.mark.unit

SPACE = "spaces/AAA"
THREAD = "spaces/AAA/threads/TTT"
USER = "users/123"
MESSAGE = "spaces/AAA/messages/M1"
APP_ID = "CA-20260115-1736899200123"
OTHER_ID = "CA-20260220-1771588800000"
PROJECT = "lab5-gemini-dev1"
TASKS_ACCOUNT = "chat-tasks@lab5-gemini-dev1.iam.gserviceaccount.com"
_IAP_EMAIL = "X-Goog-Authenticated-User-Email"


class FakeRuntime:
    def __init__(self, answer: str = "Max LTV is 80%.") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        self.error: HandlerError | None = None

    def stream_query(self, *, user_id: str, session_id: str, message: str) -> str:
        self.calls.append((user_id, session_id, message))
        if self.error is not None:
            raise self.error
        return self.answer


class FakeTasks:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, object]] = {}
        self.calls = 0
        self.error: HandlerError | None = None

    def enqueue(self, *, task_id: str, payload: object) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if task_id in self.tasks:
            return
        assert isinstance(payload, dict)
        self.tasks[task_id] = payload


class IdempotentPoster:
    """Same requestId returns the first message and does not change its text."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, object]] = {}
        self.calls: list[dict[str, str]] = []

    def create_message(
        self,
        *,
        parent: str,
        thread: str,
        text: str,
        request_id: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "parent": parent,
                "thread": thread,
                "text": text,
                "request_id": request_id,
            }
        )
        existing = self.stored.get(request_id)
        if existing is not None:
            return dict(existing)
        message = {
            "name": f"{parent}/messages/{request_id}",
            "text": text,
            "thread": {"name": thread},
        }
        self.stored[request_id] = message
        return dict(message)


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
        attempts: int = 4,
    ) -> RestResponse:
        self.calls.append((method, url, json_body, timeout, attempts))
        parsed: object | None = None
        if self.text:
            try:
                parsed = json.loads(self.text)
            except ValueError:
                parsed = None
        return RestResponse(status_code=self.status_code, json=parsed, text=self.text)


def _event(
    *,
    event_type: str = "MESSAGE",
    text: str = "@Officer What is max LTV?",
    argument: str = " What is max LTV?",
    user: str = USER,
    space: str = SPACE,
    thread: str = THREAD,
    sender_type: str = "HUMAN",
    message_name: str = MESSAGE,
) -> dict[str, object]:
    return {
        "type": event_type,
        "user": {"name": user, "type": "HUMAN"},
        "space": {"name": space},
        "message": {
            "name": message_name,
            "text": text,
            "argumentText": argument,
            "sender": {"name": user, "type": sender_type},
            "space": {"name": space},
            "thread": {"name": thread},
        },
    }


def _install_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Google's verifier. Token text selects the claim or the failure."""

    def verify(token: str, audience: str) -> dict[str, str]:
        assert audience == CHAT_URL
        if token == "tasks":
            return {"email": TASKS_ACCOUNT, "aud": audience}
        if token == "system":
            return {"email": CHAT_SYSTEM_ACCOUNT, "aud": audience}
        if token == "other-project":
            return {
                "email": "chat-tasks@other-project.iam.gserviceaccount.com",
                "aud": audience,
            }
        if token == "wrong-audience":
            raise ValueError("Token has wrong audience.")
        raise ValueError("Could not verify token signature.")

    monkeypatch.setattr(chat_main, "verify_google_id_token", verify)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_v4_enqueue_then_ack_does_not_call_stream_query() -> None:
    runtime = FakeRuntime("Max LTV is 80% (CP-RML-2026-01).")
    tasks = FakeTasks()
    status, reply = http_reply(json.dumps(_event()).encode(), tasks)
    assert status == 200
    assert reply == {"text": ACK_TEXT, "thread": {"name": THREAD}}
    assert runtime.calls == []
    task_id = task_id_from_message(MESSAGE)
    assert list(tasks.tasks) == [task_id]
    assert task_id == "spaces_AAA_messages_M1"
    assert session_id(SPACE, THREAD) == "spaces-aaa-spaces-aaa-threads-ttt"


def test_v19_session_id() -> None:
    space = "spaces/AAQA"
    thread = "spaces/AAQA/threads/BB"
    folded = "spaces-aaqa-spaces-aaqa-threads-bb"
    assert session_id(space, thread) == folded
    assert session_id(space.lower(), thread.lower()) == folded
    assert folded == folded.lower()
    assert len(folded) <= 63
    assert folded[0].isalpha()
    assert folded[-1].isalnum()

    at_limit = "t" * 54
    assert session_id("spaces/A", at_limit) == f"spaces-a-{at_limit}"
    assert len(session_id("spaces/A", at_limit)) == 63

    over = "spaces/AAQA/threads/" + ("B" * 80)
    digest = hashlib.sha256(f"{space}\n{over}".encode()).hexdigest()
    hashed = session_id(space, over)
    assert hashed == f"c{digest[:62]}"
    assert len(hashed) == 63
    assert hashed == hashed.lower()
    assert hashed[0].isalpha()
    assert hashed[-1].isalnum()
    assert hashed == session_id(space, over)


def test_other_thread_is_a_different_session() -> None:
    tasks = FakeTasks()
    other = "spaces/BBB/threads/ZZZ"
    status, reply = http_reply(
        json.dumps(
            _event(
                space="spaces/BBB",
                thread=other,
                user="users/9",
                message_name="spaces/BBB/messages/M9",
            )
        ).encode(),
        tasks,
    )
    assert status == 200
    assert reply["thread"] == {"name": other}
    payload = tasks.tasks["spaces_BBB_messages_M9"]
    assert isinstance(payload["message"], dict)
    assert payload["message"]["thread"] == {"name": other}


def test_non_message_and_bot_do_not_enqueue() -> None:
    tasks = FakeTasks()
    status, reply = http_reply(
        json.dumps(_event(event_type="ADDED_TO_SPACE")).encode(), tasks
    )
    assert status == 200
    assert reply == {}
    status, reply = http_reply(json.dumps(_event(sender_type="BOT")).encode(), tasks)
    assert status == 200
    assert reply == {}
    assert tasks.calls == 0


def test_enqueue_failure_is_not_an_ack() -> None:
    tasks = FakeTasks()
    tasks.error = HandlerError("queue down")
    status, reply = http_reply(json.dumps(_event()).encode(), tasks)
    assert status == 500
    assert reply == {"text": ENQUEUE_FAILED_TEXT}
    assert ACK_TEXT not in json.dumps(reply)
    assert tasks.tasks == {}


def test_duplicate_task_id_does_not_start_a_second_officer_call() -> None:
    tasks = FakeTasks()
    runtime = FakeRuntime("decision: accept")
    poster = IdempotentPoster()
    body = json.dumps(
        _event(
            text=f"Evaluate {APP_ID}",
            argument=f"Evaluate {APP_ID}",
        )
    ).encode()
    first_status, first = http_reply(body, tasks)
    second_status, second = http_reply(body, tasks)
    assert first_status == second_status == 200
    assert first["text"] == second["text"] == ACK_TEXT
    assert tasks.calls == 2
    assert len(tasks.tasks) == 1
    event = tasks.tasks[task_id_from_message(MESSAGE)]
    assert isinstance(event, dict)
    judge_event(event, runtime, poster)
    assert runtime.calls == [
        (USER, session_id(SPACE, THREAD), f"Evaluate {APP_ID}"),
    ]
    http_reply(body, tasks)
    assert len(tasks.tasks) == 1
    assert len(runtime.calls) == 1


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
    method, called_url, payload, timeout, _attempts = rest.calls[0]
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


def test_zero_ids_post_help_and_do_not_call_stream_query() -> None:
    runtime = FakeRuntime("should not run")
    poster = IdempotentPoster()
    judge_event(_event(), runtime, poster)
    assert runtime.calls == []
    assert poster.calls[0]["text"] == HELP_TEXT
    assert "CA-{YYYYMMDD}-{unix_ms}" in HELP_TEXT
    assert APP_ID in HELP_TEXT
    assert poster.calls[0]["parent"] == SPACE
    assert poster.calls[0]["thread"] == THREAD
    assert poster.calls[0]["request_id"] == outcome_request_id(MESSAGE, "help")


def test_repeated_single_id_is_one_match() -> None:
    runtime = FakeRuntime("decision: accept")
    poster = IdempotentPoster()
    text = f"{APP_ID} and again {APP_ID}"
    judge_event(_event(text=text, argument=text), runtime, poster)
    assert len(runtime.calls) == 1
    assert "No judgement." not in str(poster.calls[0]["text"])


def test_many_ids_are_listed_and_not_judged() -> None:
    runtime = FakeRuntime("decision: accept")
    poster = IdempotentPoster()
    text = f"Compare {OTHER_ID} with {APP_ID}"
    judge_event(_event(text=text, argument=text), runtime, poster)
    assert runtime.calls == []
    posted = poster.calls[0]["text"]
    assert APP_ID in posted
    assert OTHER_ID in posted
    assert posted.index(OTHER_ID) < posted.index(APP_ID)
    assert "No judgement." in posted
    assert poster.calls[0]["request_id"] == outcome_request_id(MESSAGE, "list")


def test_one_id_calls_stream_query_and_posts_officer_text() -> None:
    answer = "decision: missing-data. The file is incomplete."
    runtime = FakeRuntime(answer)
    poster = IdempotentPoster()
    question = f"Evaluate client application {APP_ID} against published credit policy."
    judge_event(_event(text=question, argument=question), runtime, poster)
    assert runtime.calls == [(USER, session_id(SPACE, THREAD), question)]
    assert poster.calls[0]["text"] == answer
    assert poster.calls[0]["request_id"] == outcome_request_id(MESSAGE, "judgement")
    assert poster.calls[0]["request_id"] != outcome_request_id(MESSAGE, "help")
    assert poster.calls[0]["request_id"] != outcome_request_id(MESSAGE, "failure")


def test_officer_failure_posts_an_error_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verifier(monkeypatch)
    runtime = FakeRuntime()
    runtime.error = HandlerError("streamQuery failed: HTTP 404 nope")
    poster = IdempotentPoster()
    question = f"Evaluate {APP_ID}"
    status, reply = http_judge(
        json.dumps(_event(text=question, argument=question)).encode(),
        runtime,
        poster,
        _bearer("tasks"),
        PROJECT,
    )
    assert status == 200
    assert reply == {}
    assert poster.calls[0]["text"] == OFFICER_ERROR_TEXT
    assert "decision" not in OFFICER_ERROR_TEXT
    assert poster.calls[0]["request_id"] == outcome_request_id(MESSAGE, "failure")
    assert poster.calls[0]["request_id"] != outcome_request_id(MESSAGE, "judgement")


def test_create_idempotency_keeps_the_first_text() -> None:
    runtime = FakeRuntime("decision: accept")
    poster = IdempotentPoster()
    question = f"Evaluate {APP_ID}"
    event = _event(text=question, argument=question)
    judge_event(event, runtime, poster)
    runtime.answer = "decision: reject"
    judge_event(event, runtime, poster)
    assert len(poster.stored) == 1
    stored = next(iter(poster.stored.values()))
    assert stored["text"] == "decision: accept"
    assert poster.calls[0]["request_id"] == poster.calls[1]["request_id"]
    assert [call["request_id"] for call in poster.calls] == [
        outcome_request_id(MESSAGE, "judgement"),
        outcome_request_id(MESSAGE, "judgement"),
    ]


def _judge(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    *,
    question: str | None = None,
) -> tuple[int, dict[str, object], FakeRuntime, IdempotentPoster]:
    _install_verifier(monkeypatch)
    runtime = FakeRuntime("decision: accept")
    poster = IdempotentPoster()
    text = question if question is not None else f"Evaluate {APP_ID}"
    status, reply = http_judge(
        json.dumps(_event(text=text, argument=text)).encode(),
        runtime,
        poster,
        headers,
        PROJECT,
    )
    return status, reply, runtime, poster


def test_judge_route_accepts_a_verified_chat_tasks_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = f"Evaluate {APP_ID}"
    status, reply, runtime, poster = _judge(
        monkeypatch,
        {
            **_bearer("tasks"),
            _IAP_EMAIL: f"accounts.google.com:{CHAT_SYSTEM_ACCOUNT}",
        },
        question=question,
    )
    assert status == 200
    assert reply == {}
    assert runtime.calls == [(USER, session_id(SPACE, THREAD), question)]
    assert poster.calls[0]["text"] == "decision: accept"


def test_judge_route_rejects_an_unsigned_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, poster = _judge(
        monkeypatch,
        {
            "Authorization": "Bearer unsigned.payload.sig",
            _IAP_EMAIL: f"accounts.google.com:{TASKS_ACCOUNT}",
        },
    )
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []
    assert poster.calls == []


def test_judge_route_rejects_a_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, poster = _judge(
        monkeypatch,
        {_IAP_EMAIL: f"accounts.google.com:{TASKS_ACCOUNT}"},
    )
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []
    assert poster.calls == []


def test_judge_route_rejects_a_bad_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, _poster = _judge(monkeypatch, _bearer("bad-signature"))
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []


def test_judge_route_rejects_a_wrong_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, _poster = _judge(monkeypatch, _bearer("wrong-audience"))
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []


def test_judge_route_rejects_chat_system_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, poster = _judge(monkeypatch, _bearer("system"))
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []
    assert poster.calls == []


def test_judge_route_rejects_a_verified_token_for_another_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, reply, runtime, poster = _judge(monkeypatch, _bearer("other-project"))
    assert status == 403
    assert reply == {"text": "Forbidden."}
    assert runtime.calls == []
    assert poster.calls == []


def test_v18_judge_does_not_read_iap_user_email() -> None:
    source = (repo_root() / "chat" / "main.py").read_text()
    assert _IAP_EMAIL not in source


def test_v18_verify_google_id_token_checks_the_chat_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake(token, request, audience=None, clock_skew_in_seconds=0):
        captured["token"] = token
        captured["audience"] = audience
        assert request is not None
        return {"iss": "https://accounts.google.com", "email": TASKS_ACCOUNT}

    import google.oauth2.id_token as id_token_mod

    monkeypatch.setattr(id_token_mod, "verify_oauth2_token", fake)
    claims = chat_main.verify_google_id_token("signed-token", CHAT_URL)
    assert captured == {"token": "signed-token", "audience": CHAT_URL}
    assert claims["email"] == TASKS_ACCOUNT


def test_cloud_tasks_already_exists_does_not_raise() -> None:
    rest = FakeRest("{}", status_code=409)
    config = ChatHandlerConfig("lab5-gemini-dev1", "us-east1", "99")
    CloudTasksEnqueuer(config, client=rest).enqueue(
        task_id=task_id_from_message(MESSAGE),
        payload=_event(),
    )
    method, url, body, timeout, attempts = rest.calls[0]
    assert method == "POST"
    assert url == (
        "https://cloudtasks.googleapis.com/v2/projects/lab5-gemini-dev1/"
        "locations/us-east1/queues/lab5-gemini-dev1-chat/tasks"
    )
    assert isinstance(body, dict)
    task = body["task"]
    assert timeout == 5.0
    assert attempts == 1
    assert task["name"].endswith("/tasks/spaces_AAA_messages_M1")
    http_request = task["httpRequest"]
    assert http_request["url"] == f"{CHAT_URL}/tasks/judge"
    assert http_request["oidcToken"]["audience"] == CHAT_URL
    assert http_request["oidcToken"]["serviceAccountEmail"] == (
        "chat-tasks@lab5-gemini-dev1.iam.gserviceaccount.com"
    )
    decoded = json.loads(base64.b64decode(http_request["body"]))
    assert decoded["message"]["name"] == MESSAGE
    failed = FakeRest("nope", status_code=500)
    with pytest.raises(HandlerError, match="Cloud Tasks enqueue"):
        CloudTasksEnqueuer(config, client=failed).enqueue(
            task_id="spaces_AAA_messages_M1",
            payload=_event(),
        )


def test_create_url_uses_stable_request_id_and_thread() -> None:
    rest = FakeRest(json.dumps({"name": f"{SPACE}/messages/created", "text": "kept"}))
    poster = RestChatPoster(
        ChatHandlerConfig("lab5-gemini-dev1", "us-east1", "99"),
        client=rest,
    )
    request_id = outcome_request_id(MESSAGE, "judgement")
    created = poster.create_message(
        parent=SPACE,
        thread=THREAD,
        text="decision: accept",
        request_id=request_id,
    )
    assert created["text"] == "kept"
    _method, url, body, _timeout, _attempts = rest.calls[0]
    assert url.startswith(f"https://chat.googleapis.com/v1/{SPACE}/messages?")
    assert f"requestId={request_id}" in url
    assert "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in url
    assert body == {"text": "decision: accept", "thread": {"name": THREAD}}


def test_queue_name_is_composed_from_the_project() -> None:
    assert queue_name("lab5-gemini-dev1") == "lab5-gemini-dev1-chat"
    assert tasks_invoker_email("lab5-gemini-dev1") == (
        "chat-tasks@lab5-gemini-dev1.iam.gserviceaccount.com"
    )
    config = ChatHandlerConfig("lab5-gemini-dev1", "us-east1", "99")
    body = task_http_body(config, "spaces_AAA_messages_M1", {"type": "MESSAGE"})
    assert body["task"]["name"] == (
        "projects/lab5-gemini-dev1/locations/us-east1/"
        "queues/lab5-gemini-dev1-chat/tasks/spaces_AAA_messages_M1"
    )


def test_application_id_scan_is_exact() -> None:
    assert application_ids("What is max LTV?") == []
    assert application_ids("status for Ada Lovelace") == []
    assert application_ids(f"see {APP_ID}.") == [APP_ID]
    assert application_ids(f"X{APP_ID}") == []
    assert application_ids(f"{APP_ID}extra") == []
    assert application_ids(f"{OTHER_ID} {APP_ID} {OTHER_ID}") == [OTHER_ID, APP_ID]


def test_invalid_json_does_not_enqueue() -> None:
    tasks = FakeTasks()
    status, reply = http_reply(b"{", tasks)
    assert status == 400
    assert reply["text"] == "Invalid JSON."
    assert tasks.calls == 0


def test_http_post_acks_then_judge_posts_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verifier(monkeypatch)
    runtime = FakeRuntime("cited answer")
    tasks = FakeTasks()
    poster = IdempotentPoster()
    server = serve(runtime, "127.0.0.1", 0, tasks=tasks, poster=poster, project=PROJECT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        question = f"Evaluate {APP_ID}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=json.dumps(_event(text=question, argument=question)).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
            assert response.status == 200
        assert payload == {"text": ACK_TEXT, "thread": {"name": THREAD}}
        assert runtime.calls == []
        event = next(iter(tasks.tasks.values()))
        judge = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/judge",
            data=json.dumps(event).encode(),
            headers={
                "Content-Type": "application/json",
                **_bearer("tasks"),
            },
            method="POST",
        )
        with urllib.request.urlopen(judge, timeout=5) as response:
            assert response.status == 200
        assert runtime.calls == [(USER, session_id(SPACE, THREAD), question)]
        assert poster.calls[0]["text"] == "cited answer"
        denied = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/judge",
            data=json.dumps(event).encode(),
            headers={
                "Content-Type": "application/json",
                **_bearer("system"),
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied, timeout=5)
        assert caught.value.code == 403
        assert len(runtime.calls) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_missing_reasoning_engine_exits_2() -> None:
    with pytest.raises(HandlerError, match="REASONING_ENGINE") as caught:
        config_from_env(
            {
                "GOOGLE_CLOUD_PROJECT": "lab5-gemini-dev1",
                "GOOGLE_CLOUD_LOCATION": "us-east1",
            }
        )
    assert caught.value.exit_code == 2


def test_config_reads_only_the_supplied_env() -> None:
    resource = "projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/9"
    config = config_from_env(
        {
            "GOOGLE_CLOUD_PROJECT": "lab5-gemini-dev1",
            "GOOGLE_CLOUD_LOCATION": "us-east1",
            "REASONING_ENGINE": resource,
            "GCS_BUCKET": "ignored",
        }
    )
    assert config.project == "lab5-gemini-dev1"
    assert config.location == "us-east1"
    assert config.reasoning_engine == resource


def test_bind_address_uses_port_env_or_flag() -> None:
    assert bind_address([], {}) == ("0.0.0.0", 8080)
    assert bind_address([], {"PORT": "9090"}) == ("0.0.0.0", 9090)
    assert bind_address(["--port", "7"], {"PORT": "9090"}) == ("0.0.0.0", 7)
    assert bind_address(["--host", "127.0.0.1", "--port", "8081"], {}) == (
        "127.0.0.1",
        8081,
    )


def test_v18_handler_is_standalone() -> None:
    root = repo_root()
    assert not (root / "src/docgen/google_chat.py").exists()
    source = (root / "chat" / "main.py").read_text(encoding="utf-8")
    for needle in (
        "credit-policy-agent.instructions",
        "facts.yaml",
        "generateContent",
        "Policy ID",
        "That is not in the published policies",
        "infra/outputs.json",
        "docgen",
        "google-adk",
        "google.adk",
    ):
        assert needle not in source, needle
    requirements = [
        line.strip()
        for line in (root / "chat" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert requirements == ["google-auth", "requests"]


def test_teams_marker_is_not_hard_skipped() -> None:
    conftest = (repo_root() / "tests/conftest.py").read_text(encoding="utf-8")
    assert "Google Chat is not part of v1" not in conftest
    assert "pytest_runtest_setup" not in conftest
    data = (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    assert "teams: Google Chat live path" in data
    assert "not part of v1" not in data
