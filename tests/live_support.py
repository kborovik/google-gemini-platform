from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
import requests

from docgen.application import iter_manifest_documents
from docgen.constants import APPLICATION_TYPES
from docgen.env import repo_root
from tests.helpers import APPLICATION_OUTPUT_RELATIVE

# Public Chat handler. Cloud Run accepts the active gcloud user's identity token.
CHAT_URL = "https://credit-policy.ai.lab5.ca/"
# streamQuery budget is 180s; the Cloud Run service timeout is 300s.
CHAT_TIMEOUT_SECONDS = 240.0
# Gemini 429s inside an HTTP 200 streamQuery body. Wait, then send the turn again.
_QUOTA_RETRY_WAITS = (30.0, 60.0)
_E2E_SPACE = "spaces/e2e"
_E2E_USER = "users/e2e"
# `make e2e` queries the reasoning engine, then the same filings through chat.
ENGINE_SURFACE = "google_vertex_ai_reasoning_engine"
CHAT_SURFACE = "google_cloud_run_v2_service.chat"
AGENT_SURFACES = (
    ENGINE_SURFACE,
    CHAT_SURFACE,
)
_ENGINE_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "REASONING_ENGINE",
)


ACK_TEXT = "Request received."


def chat_message_event(
    user_text: str,
    *,
    thread: str,
    space: str = _E2E_SPACE,
    user: str = _E2E_USER,
    message_name: str | None = None,
) -> dict[str, Any]:
    """Google Chat MESSAGE the public handler accepts."""
    return {
        "type": "MESSAGE",
        "user": {"name": user, "type": "HUMAN"},
        "space": {"name": space},
        "message": {
            "name": message_name or f"{thread}/messages/e2e",
            "text": user_text,
            "argumentText": user_text,
            "sender": {"name": user, "type": "HUMAN"},
            "space": {"name": space},
            "thread": {"name": thread},
        },
    }


class ChatListError(AssertionError):
    """spaces.messages.list failed. Do not poll this error."""


def require_chat_thread(env: Mapping[str, str]) -> tuple[str, str]:
    """Real space and thread. Synthetic ids are not Chat resources."""
    space = env.get("CHAT_SPACE", "").strip()
    thread = env.get("CHAT_THREAD", "").strip()
    if not _chat_thread_names(space, thread):
        pytest.skip("CHAT_SPACE and CHAT_THREAD must name a real Chat space and thread")
    return space, thread


def _chat_thread_names(space: str, thread: str) -> bool:
    return space.startswith("spaces/") and thread.startswith(f"{space}/threads/")


def thread_name_from_messages(messages: list[Mapping[str, Any]], *, space: str) -> str:
    """thread.name of the newest message in one space."""
    ranked: list[tuple[float, str]] = []
    for message in messages:
        thread = message.get("thread")
        name = thread.get("name") if isinstance(thread, dict) else ""
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name.startswith(f"{space}/threads/"):
            continue
        ranked.append((_create_stamp(message), name))
    if not ranked:
        raise ChatListError(f"{space} has no thread")
    ranked.sort()
    return ranked[-1][1]


def emit_chat_env(messages: list[Mapping[str, Any]] | None = None) -> None:
    """Print shell exports. A set CHAT_THREAD is kept; otherwise list the space."""
    import shlex

    space = os.environ.get("CHAT_SPACE", "").strip()
    thread = os.environ.get("CHAT_THREAD", "").strip()
    if not space.startswith("spaces/"):
        raise SystemExit("CHAT_SPACE must be spaces/{id}")
    if not thread:
        try:
            if messages is None:
                messages = list_recent_messages(space)
            thread = thread_name_from_messages(messages, space=space)
        except ChatListError as exc:
            raise SystemExit(str(exc)) from exc
    if not _chat_thread_names(space, thread):
        raise SystemExit("CHAT_THREAD must be {CHAT_SPACE}/threads/{thread}")
    print(f"export CHAT_SPACE={shlex.quote(space)}")
    print(f"export CHAT_THREAD={shlex.quote(thread)}")


def thread_list_filter(thread: str, *, not_before: float | None = None) -> str:
    """thread.name stays unquoted. createTime is a quoted RFC3339 timestamp."""
    clause = f"thread.name = {thread}"
    if not_before is None:
        return clause
    stamp = datetime.fromtimestamp(not_before - 5.0, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return f'createTime > "{stamp}" AND {clause}'


def follow_up_text(
    messages: list[Mapping[str, Any]],
    *,
    not_before: float | None = None,
) -> str:
    """Officer, help, or error text from Chat. The HTTP body is only the ack."""
    texts: list[str] = []
    for message in messages:
        sender = message.get("sender")
        if isinstance(sender, dict) and sender.get("type") == "HUMAN":
            continue
        if not_before is not None and not _created_after(message, not_before):
            continue
        text = message.get("text")
        if isinstance(text, str) and text.strip() and text.strip() != ACK_TEXT:
            texts.append(text.strip())
    if not texts:
        raise AssertionError("chat follow-up message is missing")
    return texts[-1]


def _created_after(message: Mapping[str, Any], not_before: float) -> bool:
    created = message.get("createTime")
    if not isinstance(created, str) or not created.strip():
        return False
    stamp = created.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() >= not_before - 5.0


def gcloud_identity_token() -> str:
    """Identity token for the active gcloud user. User accounts cannot set --audiences."""
    try:
        completed = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        pytest.fail(f"gcloud auth print-identity-token failed: {exc}")
    token = completed.stdout.strip()
    if completed.returncode != 0 or not token:
        detail = completed.stderr.strip() or "empty token"
        pytest.fail(f"gcloud auth print-identity-token failed: {detail}")
    return token


def quota_exhausted(body: Mapping[str, Any]) -> bool:
    text = body.get("text")
    return isinstance(text, str) and "RESOURCE_EXHAUSTED" in text


def post_chat_message(
    user_text: str,
    *,
    thread: str,
    space: str = _E2E_SPACE,
    user: str = _E2E_USER,
    message_name: str | None = None,
) -> dict[str, Any]:
    """POST one Chat MESSAGE to the public handler and return its JSON body."""
    waits = (0.0, *_QUOTA_RETRY_WAITS)
    body: dict[str, Any] | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        body = _post_chat_once(
            user_text,
            thread=thread,
            space=space,
            user=user,
            message_name=message_name,
        )
        if not quota_exhausted(body) or attempt == len(waits) - 1:
            return body
    assert body is not None
    return body


def _post_chat_once(
    user_text: str,
    *,
    thread: str,
    space: str,
    user: str,
    message_name: str | None = None,
) -> dict[str, Any]:
    try:
        response = requests.post(
            CHAT_URL,
            headers={
                "Authorization": f"Bearer {gcloud_identity_token()}",
                "Content-Type": "application/json",
            },
            json=chat_message_event(
                user_text,
                thread=thread,
                space=space,
                user=user,
                message_name=message_name,
            ),
            timeout=CHAT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        pytest.fail(f"POST {CHAT_URL} failed: {exc}")
    try:
        body = response.json()
    except ValueError:
        pytest.fail(
            f"POST {CHAT_URL} returned {response.status_code} "
            f"non-JSON body: {response.text[:500]}"
        )
    if response.status_code != 200 or not isinstance(body, dict):
        pytest.fail(f"POST {CHAT_URL} returned {response.status_code}: {body}")
    return body


def invoke_agent(env: dict[str, str], user_text: str) -> str:
    """Ask the deployed officer. The judgement is the Chat follow-up, not the ack."""
    if not env.get("GOOGLE_CLOUD_PROJECT"):
        pytest.fail("GOOGLE_CLOUD_PROJECT is not set")
    space, thread = require_chat_thread(env)
    message_name = f"{thread}/messages/{uuid.uuid4().hex}"
    started = time.time()
    body = post_chat_message(
        user_text,
        thread=thread,
        space=space,
        message_name=message_name,
    )
    ack = body.get("text")
    if ack != ACK_TEXT:
        pytest.fail(f"chat host ack was not {ACK_TEXT!r}: {body}")
    text = read_follow_up(space=space, thread=thread, not_before=started)
    if quota_exhausted({"text": text}):
        pytest.fail(f"chat follow-up exhausted quota: {text}")
    print_agent_turn(
        user_text,
        text,
        surface=CHAT_SURFACE,
    )
    return text


def read_follow_up(
    *,
    space: str,
    thread: str,
    timeout: float = 200.0,
    not_before: float | None = None,
) -> str:
    """Poll spaces.messages.list until the worker's follow-up is present."""
    deadline = time.monotonic() + timeout
    last_error = "chat follow-up message is missing"
    while True:
        try:
            messages = list_thread_messages(
                space=space, thread=thread, not_before=not_before
            )
        except ChatListError as exc:
            pytest.fail(str(exc))
        try:
            return follow_up_text(messages, not_before=not_before)
        except ChatListError as exc:
            pytest.fail(str(exc))
        except AssertionError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            pytest.fail(f"follow-up message not posted: {last_error}")
        time.sleep(2.0)


# spaces.messages.list does not accept chat.bot. User auth lists the thread.
_CHAT_LIST_SCOPE = "https://www.googleapis.com/auth/chat.messages.readonly"
_LIST_PAGE_CAP = 20


def list_recent_messages(space: str, *, page_size: int = 10) -> list[dict[str, Any]]:
    """Newest messages in a space. One page is enough to name a thread."""
    try:
        response = requests.get(
            f"https://chat.googleapis.com/v1/{space}/messages",
            headers={"Authorization": f"Bearer {_chat_list_token()}"},
            params={"pageSize": page_size, "orderBy": "createTime DESC"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ChatListError(f"spaces.messages.list failed: {exc}") from exc
    if response.status_code != 200:
        raise ChatListError(
            f"spaces.messages.list returned {response.status_code}: "
            f"{response.text[:500]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ChatListError("spaces.messages.list returned non-JSON") from exc
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def list_thread_messages(
    *, space: str, thread: str, not_before: float | None = None
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    page_token = ""
    list_filter = thread_list_filter(thread, not_before=not_before)
    for _page in range(_LIST_PAGE_CAP):
        params: dict[str, str | int] = {
            "filter": list_filter,
            "pageSize": 50,
            "orderBy": "createTime DESC",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = requests.get(
                f"https://chat.googleapis.com/v1/{space}/messages",
                headers={"Authorization": f"Bearer {_chat_list_token()}"},
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ChatListError(f"spaces.messages.list failed: {exc}") from exc
        if response.status_code != 200:
            raise ChatListError(
                f"spaces.messages.list returned {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AssertionError("spaces.messages.list returned non-JSON") from exc
        if not isinstance(body, dict):
            break
        messages = body.get("messages")
        if isinstance(messages, list):
            collected.extend(item for item in messages if isinstance(item, dict))
        page_token = body.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            break
    collected.sort(key=_create_stamp)
    return collected


def _chat_list_token() -> str:
    """User token for spaces.messages.list. chat.bot cannot call that method."""
    import google.auth
    import google.auth.exceptions
    import google.auth.transport.requests

    try:
        credentials, _detected = google.auth.default(scopes=[_CHAT_LIST_SCOPE])
        credentials.refresh(google.auth.transport.requests.Request())
    except google.auth.exceptions.GoogleAuthError as exc:
        raise ChatListError(
            f"spaces.messages.list needs a user token with {_CHAT_LIST_SCOPE}: {exc}"
        ) from exc
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token:
        raise ChatListError("chat list token was not issued")
    return token


def _create_stamp(message: Mapping[str, Any]) -> float:
    created = message.get("createTime")
    if not isinstance(created, str) or not created.strip():
        return 0.0
    stamp = created.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def invoke_reasoning_engine(env: dict[str, str], user_text: str) -> str:
    """Ask the deployed officer through reasoningEngines/{id}:streamQuery."""
    missing = [name for name in _ENGINE_ENV if not env.get(name)]
    if missing:
        pytest.fail("reasoning engine env missing: " + ", ".join(missing))
    chat = _chat_main()
    try:
        config = chat.config_from_env(env)
    except chat.HandlerError as exc:
        pytest.fail(str(exc))
    runtime = chat.RestAgentRuntime(config)
    session = f"e2e-{uuid.uuid4().hex}"
    waits = (0.0, *_QUOTA_RETRY_WAITS)
    for attempt, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        try:
            text = runtime.stream_query(
                user_id="users-e2e",
                session_id=session,
                message=user_text,
            )
        except chat.HandlerError as exc:
            message = str(exc)
            if "RESOURCE_EXHAUSTED" in message and attempt < len(waits) - 1:
                continue
            pytest.fail(f"reasoning engine streamQuery failed: {message}")
        if not isinstance(text, str) or not text.strip():
            pytest.fail("reasoning engine returned no text")
        if "RESOURCE_EXHAUSTED" in text and attempt < len(waits) - 1:
            continue
        if "RESOURCE_EXHAUSTED" in text:
            pytest.fail(f"reasoning engine streamQuery failed: {text}")
        print_agent_turn(
            user_text,
            text,
            surface=ENGINE_SURFACE,
        )
        return text
    pytest.fail("reasoning engine streamQuery failed")


def invoke_on_surface(env: dict[str, str], user_text: str, surface: str) -> str:
    """Query one deployed surface. Engine and chat stay separate calls."""
    if surface == ENGINE_SURFACE:
        return invoke_reasoning_engine(env, user_text)
    if surface == CHAT_SURFACE:
        return invoke_agent(env, user_text)
    pytest.fail(f"unknown agent surface: {surface}")


def print_agent_turn(request: str, response: str, *, surface: str = "") -> None:
    header = f"\n{surface}\n" if surface else "\n"
    sys.stdout.write(
        f"{header}Agent Request\n{request}\n\nAgent Response\n{response}\n"
    )
    sys.stdout.flush()


def _chat_main() -> Any:
    cached = sys.modules.get("chat_main")
    if cached is not None:
        return cached
    path = repo_root() / "chat" / "main.py"
    spec = importlib.util.spec_from_file_location("chat_main", path)
    if spec is None or spec.loader is None:
        pytest.fail(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["chat_main"] = module
    spec.loader.exec_module(module)
    return module


# `make e2e` judges this many newest generated filings.
LATEST_GENERATED_COUNT = 3


def latest_generated_cases(
    cases: list[dict[str, str]],
    *,
    size: int = LATEST_GENERATED_COUNT,
) -> list[dict[str, str]]:
    """The newest `size` filings by application id.

    `generate application --all` appends accepted, rejected, and missing-data
    with increasing ids, so the tail is that batch. A shorter list is returned
    whole, still in id order.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    ordered = sorted(cases, key=lambda item: item["application_id"])
    if len(ordered) <= size:
        return ordered
    return ordered[-size:]


def agent_judgement_runs(
    cases: list[dict[str, str]],
) -> list[tuple[str, dict[str, str]]]:
    """Each filing on the reasoning engine, then the same filings on chat."""
    return [(surface, case) for surface in AGENT_SURFACES for case in cases]


def load_judgement_cases(root: Path | None = None) -> list[dict[str, str]]:
    """Labeled filings in data/client-applications/manifest.json."""
    manifest_path = (
        (root or repo_root()) / APPLICATION_OUTPUT_RELATIVE / "manifest.json"
    )
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        return []
    cases: list[dict[str, str]] = []
    for item in iter_manifest_documents(manifest):
        application_id = str(item.get("application_id") or "").strip()
        if not application_id:
            continue
        cases.append(
            {
                "application_id": application_id,
                "intended_outcome": str(item.get("intended_outcome") or ""),
                "expected_outcome": str(item.get("expected_outcome") or ""),
            }
        )
    return cases


def require_manifest_outcomes(
    *,
    application_id: str,
    intended_outcome: str,
    expected_outcome: str,
) -> None:
    """Both manifest fields are the same published outcome."""
    if (
        intended_outcome not in APPLICATION_TYPES
        or expected_outcome not in APPLICATION_TYPES
    ):
        raise AssertionError(
            f"{application_id}: intended_outcome {intended_outcome!r} "
            f"expected_outcome {expected_outcome!r} "
            f"must both be one of {APPLICATION_TYPES}"
        )
    if intended_outcome != expected_outcome:
        raise AssertionError(
            f"{application_id}: intended_outcome {intended_outcome!r} "
            f"!= expected_outcome {expected_outcome!r}"
        )


def assert_manifest_judgement(
    text: str,
    *,
    application_id: str,
    intended_outcome: str,
    expected_outcome: str,
) -> None:
    """Received Judgement decision matches both manifest outcome fields."""
    require_manifest_outcomes(
        application_id=application_id,
        intended_outcome=intended_outcome,
        expected_outcome=expected_outcome,
    )
    try:
        assert_expected_decision(text, expected_outcome)
    except AssertionError as exc:
        raise AssertionError(f"{application_id}: {exc}") from exc


def expected_decision_token(expected_judgement: str) -> str:
    if expected_judgement == "accepted":
        return "accept"
    if expected_judgement == "rejected":
        return "reject"
    return "missing-data"


# First Judgement `decision` field. Longer tokens first so "accepted" wins over "accept".
_LEAD_DECISION = re.compile(
    r"\bdecision\b(?:\s*\*\*)?[\s:*–=-]+[`'\"]*"
    r"(accepted|rejected|missing-data|accept|reject|missing)\b",
    re.IGNORECASE,
)


def lead_decision_token(text: str) -> str:
    match = _LEAD_DECISION.search(text)
    if match is None:
        return ""
    raw = match.group(1).lower()
    if raw.startswith("accept"):
        return "accept"
    if raw.startswith("reject"):
        return "reject"
    return "missing-data"


def assert_expected_decision(text: str, expected_judgement: str) -> None:
    expected = expected_decision_token(expected_judgement)
    lead = lead_decision_token(text)
    assert lead == expected, f"lead decision {lead!r} != {expected!r}\n{text}"
