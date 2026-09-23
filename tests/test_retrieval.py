from __future__ import annotations

import pytest

from docgen.constants import REFUSAL_SENTENCE
from tests.helpers import load_golden_queries
from tests.live_support import flatten_retrieve_text, pick_application_case, retrieve

pytestmark = pytest.mark.retrieval


def test_retrieve_residential_ltv_from_policy(live_env: dict[str, str]) -> None:
    body = retrieve(live_env, "What is max LTV on an owner-occupied mortgage?")
    blob = flatten_retrieve_text(body).lower()
    assert "80%" in blob or "ltv" in blob
    assert REFUSAL_SENTENCE not in blob


def test_retrieve_application_by_customer_name(live_env: dict[str, str]) -> None:
    record = pick_application_case()
    name = str(record["customer_name"])
    body = retrieve(live_env, f"Find client application for {name}")
    blob = flatten_retrieve_text(body)
    assert name.split()[0] in blob or str(record["application_id"]) in blob


def test_retrieval_golden_does_not_require_refusal_sentence() -> None:
    for query in load_golden_queries():
        if "retrieval" not in query.get("layers", ["retrieval", "agent"]):
            continue
        for value in query.get("expected_contains") or []:
            assert REFUSAL_SENTENCE not in str(value).lower()
