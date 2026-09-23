from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from talos.application import parse_application_markdown
from talos.chat import ChatConfig, GeminiChatModel, load_instructions
from talos.constants import DEFAULT_CHAT_MODEL, DEFAULT_CORPUS
from talos.env import repo_root
from talos.errors import TalosError
from talos.rest import RequestsRest, RestResponse, raise_for_status
from talos.search_index import data_store_resource, search_url
from tests.helpers import APPLICATION_FIXTURES_RELATIVE, APPLICATION_OUTPUT_RELATIVE


def corpus_name(env: dict[str, str]) -> str:
    return data_store_resource(env["GOOGLE_CLOUD_PROJECT"], DEFAULT_CORPUS)


def retrieve(env: dict[str, str], query: str, *, top_k: int = 5) -> dict[str, Any]:
    response = RequestsRest().request(
        "POST",
        search_url(env["GOOGLE_CLOUD_PROJECT"], DEFAULT_CORPUS),
        json_body={"query": query, "pageSize": top_k},
        timeout=120.0,
    )
    _raise_or_fail(response, "Agent Search")
    body = response.json
    return body if isinstance(body, dict) else {}


def flatten_retrieve_text(body: dict[str, Any]) -> str:
    chunks: list[str] = []
    contexts = body.get("contexts")
    rows = contexts.get("contexts") if isinstance(contexts, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("text", "sourceUri", "sourceDisplayName"):
                value = row.get(key)
                if isinstance(value, str) and value:
                    chunks.append(value)
    results = body.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            chunk = result.get("chunk")
            if isinstance(chunk, dict) and isinstance(chunk.get("content"), str):
                chunks.append(chunk["content"])
            document = result.get("document")
            if not isinstance(document, dict):
                continue
            content = document.get("content")
            if isinstance(content, dict) and isinstance(content.get("uri"), str):
                chunks.append(content["uri"])
            derived = document.get("derivedStructData")
            if not isinstance(derived, dict):
                continue
            for answer in derived.get("extractive_answers") or []:
                if isinstance(answer, dict) and isinstance(answer.get("content"), str):
                    chunks.append(answer["content"])
            snippets = derived.get("snippets")
            if isinstance(snippets, list):
                for snippet in snippets:
                    if isinstance(snippet, dict) and isinstance(
                        snippet.get("snippet"), str
                    ):
                        chunks.append(snippet["snippet"])
    return "\n".join(chunks)


def invoke_agent(env: dict[str, str], user_text: str) -> str:
    model = GeminiChatModel(
        RequestsRest(),
        ChatConfig(
            project=env["GOOGLE_CLOUD_PROJECT"],
            location=env["GOOGLE_CLOUD_LOCATION"],
            corpus_name=corpus_name(env),
            model=DEFAULT_CHAT_MODEL,
            instructions=load_instructions(),
        ),
    )
    try:
        text = model.generate(
            contents=[{"role": "user", "parts": [{"text": user_text}]}],
            system=load_instructions(),
        )
    except TalosError as exc:
        pytest.fail(str(exc))
    print_agent_turn(user_text, text)
    return text


def print_agent_turn(request: str, response: str) -> None:
    sys.stdout.write(f"\nAgent Request\n{request}\n\nAgent Response\n{response}\n")
    sys.stdout.flush()


def _raise_or_fail(response: RestResponse, action: str) -> None:
    try:
        raise_for_status(response, action)
    except TalosError as exc:
        pytest.fail(str(exc))


def _cases_from_dir(base: Path, *, fixture: bool) -> list[dict[str, Any]]:
    from talos.application import iter_manifest_documents

    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for item in iter_manifest_documents(manifest):
        filename = str(item.get("filename") or "")
        path = base / filename
        if not path.is_file():
            continue
        record = parse_application_markdown(path.read_text(encoding="utf-8"))
        slot = str(
            item.get("expected_outcome")
            or item.get("intended_outcome")
            or item.get("slot")
            or ""
        )
        record["product_family"] = item.get("product_family")
        record["intended_outcome"] = item.get("intended_outcome") or slot
        record["expected_outcome"] = item.get("expected_outcome") or slot
        record["application_type"] = slot
        record["expected_judgement"] = (
            item.get("expected_outcome") or item.get("intended_outcome") or slot
        )
        record["from_fixtures"] = fixture
        cases.append(record)
    return cases


def application_cases() -> list[dict[str, Any]]:
    """Committed fixtures first, then gitignored generated filings."""
    root = repo_root()
    cases = _cases_from_dir(root / APPLICATION_FIXTURES_RELATIVE, fixture=True)
    seen = {str(item.get("application_id")) for item in cases}
    for item in _cases_from_dir(root / APPLICATION_OUTPUT_RELATIVE, fixture=False):
        application_id = str(item.get("application_id") or "")
        if application_id and application_id not in seen:
            cases.append(item)
            seen.add(application_id)
    if not cases:
        pytest.skip(
            "no application fixtures; expected "
            f"{APPLICATION_FIXTURES_RELATIVE}/credit-application-*.md"
        )
    return cases


def pick_application_case(application_type: str | None = None) -> dict[str, Any]:
    pool = application_cases()
    if application_type is not None:
        pool = [
            item for item in pool if item.get("application_type") == application_type
        ]
    fixtures = [item for item in pool if item.get("from_fixtures")]
    if fixtures:
        pool = fixtures
    if not pool:
        pytest.skip(
            "no application fixture"
            + (f" of type {application_type}" if application_type else "")
        )
    raw_seed = os.environ.get("E2E_APPLICATION_SEED", "0")
    try:
        seed = int(raw_seed)
    except ValueError:
        seed = 0
    return random.Random(seed).choice(pool)


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

_POLICY_CITE = re.compile(
    r"CP-[A-Z]{3}-\d{4}-\d{2}|gs://\S*credit-policies/",
)
_APPLICATION_CITE = re.compile(
    r"credit-application-CA-\d{8}-\d+|gs://\S*client-applications/",
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


def citation_present(text: str) -> bool:
    return (
        _POLICY_CITE.search(text) is not None
        or _APPLICATION_CITE.search(text) is not None
    )


def assert_policy_citations(text: str) -> None:
    """Policy thresholds cite a policy file. An application hit is not enough."""
    assert _POLICY_CITE.search(text), "expected a policy citation:\n" + text
