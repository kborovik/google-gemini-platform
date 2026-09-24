from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
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


def chat_message_event(
    user_text: str,
    *,
    thread: str,
    space: str = _E2E_SPACE,
    user: str = _E2E_USER,
) -> dict[str, Any]:
    """Google Chat MESSAGE the public handler accepts."""
    return {
        "type": "MESSAGE",
        "user": {"name": user, "type": "HUMAN"},
        "space": {"name": space},
        "message": {
            "text": user_text,
            "argumentText": user_text,
            "sender": {"name": user, "type": "HUMAN"},
            "space": {"name": space},
            "thread": {"name": thread},
        },
    }


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
) -> dict[str, Any]:
    """POST one Chat MESSAGE to the public handler and return its JSON body."""
    waits = (0.0, *_QUOTA_RETRY_WAITS)
    body: dict[str, Any] | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        body = _post_chat_once(user_text, thread=thread, space=space, user=user)
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
) -> dict[str, Any]:
    try:
        response = requests.post(
            CHAT_URL,
            headers={
                "Authorization": f"Bearer {gcloud_identity_token()}",
                "Content-Type": "application/json",
            },
            json=chat_message_event(user_text, thread=thread, space=space, user=user),
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
    """Ask the deployed officer through https://credit-policy.ai.lab5.ca."""
    if not env.get("GOOGLE_CLOUD_PROJECT"):
        pytest.fail("GOOGLE_CLOUD_PROJECT is not set")
    thread = f"{_E2E_SPACE}/threads/{uuid.uuid4().hex}"
    body = post_chat_message(user_text, thread=thread)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        pytest.fail(f"chat host returned no text: {body}")
    print_agent_turn(user_text, text)
    return text


def print_agent_turn(request: str, response: str) -> None:
    sys.stdout.write(f"\nAgent Request\n{request}\n\nAgent Response\n{response}\n")
    sys.stdout.flush()


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
