from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.live_support import (
    application_cases,
    quota_exhausted,
    assert_expected_decision,
    assert_manifest_judgement,
    chat_message_event,
    expected_decision_token,
    flatten_retrieve_text,
    lead_decision_token,
    load_judgement_cases,
    pick_application_case,
    resolve_judgement_sample_seed,
    latest_generated_cases,
    sample_judgement_cases,
    print_agent_turn,
    search_request,
)

pytestmark = pytest.mark.unit


def test_quota_exhausted_matches_stream_query_429() -> None:
    assert quota_exhausted({"text": "streamQuery failed: 429 RESOURCE_EXHAUSTED."})
    assert not quota_exhausted({"text": "decision: accept"})


def test_chat_message_event_is_a_human_message() -> None:
    thread = "spaces/e2e/threads/abc"
    event = chat_message_event("Evaluate CA-1", thread=thread)
    assert event["type"] == "MESSAGE"
    assert event["user"] == {"name": "users/e2e", "type": "HUMAN"}
    message = event["message"]
    assert message["argumentText"] == "Evaluate CA-1"
    assert message["text"] == "Evaluate CA-1"
    assert message["sender"] == {"name": "users/e2e", "type": "HUMAN"}
    assert message["space"] == {"name": "spaces/e2e"}
    assert message["thread"] == {"name": thread}


def test_search_request_asks_for_snippets_not_extractive_answers() -> None:
    body = search_request("max LTV", top_k=5)
    assert body["query"] == "max LTV"
    assert body["pageSize"] == 5
    assert body["contentSearchSpec"] == {"snippetSpec": {"returnSnippet": True}}
    assert "extractiveContentSpec" not in body["contentSearchSpec"]


def test_flatten_reads_search_snippets_and_link() -> None:
    text = flatten_retrieve_text(
        {
            "results": [
                {
                    "document": {
                        "derivedStructData": {
                            "link": "gs://bucket/credit-policies/CP-RML.md",
                            "title": "CP-RML-2026-01-residential-mortgage",
                            "snippets": [
                                {
                                    "snippet": "Maximum <b>LTV</b> is 80%.",
                                    "snippet_status": "SUCCESS",
                                }
                            ],
                        }
                    }
                }
            ]
        }
    )
    assert "Maximum <b>LTV</b> is 80%." in text
    assert "gs://bucket/credit-policies/CP-RML.md" in text
    assert "CP-RML-2026-01-residential-mortgage" in text


def test_print_agent_turn_labels_request_and_response(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_agent_turn(
        "Evaluate client application CA-20260914-1.",
        "I judge this application as accepted.",
    )
    out = capsys.readouterr().out
    request_i = out.index("Agent Request")
    prompt_i = out.index("Evaluate client application CA-20260914-1.")
    response_i = out.index("Agent Response")
    reply_i = out.index("I judge this application as accepted.")
    assert request_i < prompt_i < response_i < reply_i
    assert out.endswith("\n")


def test_application_cases_include_committed_fixtures() -> None:
    cases = application_cases()
    ids = {str(item["application_id"]) for item in cases}
    assert "CA-20260115-1768478400000" in ids
    kinds = {str(item["application_type"]) for item in cases}
    assert kinds >= {"accepted", "rejected", "missing-data"}
    helene = next(
        item for item in cases if item["application_id"] == "CA-20260115-1768478400000"
    )
    assert helene["expected_outcome"] == "accepted"
    assert helene["facility"]["appraisal_date"] == "2026-08-01"
    assert helene["from_fixtures"] is True


def test_pick_application_case_is_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_APPLICATION_SEED", "0")
    first = pick_application_case("accepted")
    second = pick_application_case("accepted")
    assert first["application_id"] == second["application_id"]
    assert first["application_type"] == "accepted"
    assert first["from_fixtures"] is True


def test_expected_decision_token_maps_slots() -> None:
    assert expected_decision_token("accepted") == "accept"
    assert expected_decision_token("rejected") == "reject"
    assert expected_decision_token("missing-data") == "missing-data"


def test_lead_decision_token_reads_first_decision_field() -> None:
    assert lead_decision_token("decision: accept. LTV clears.") == "accept"
    assert lead_decision_token("1) decision: rejected — LTV 72%.") == "reject"
    assert lead_decision_token("**Decision:** missing-data") == "missing-data"
    assert lead_decision_token("- **decision**: accept") == "accept"
    assert lead_decision_token("**Decision:** `reject`") == "reject"
    assert lead_decision_token("I judge this application as accepted.") == ""


def test_missing_data_lead_does_not_satisfy_rejected() -> None:
    text = (
        "1) decision: missing-data – the file is incomplete (a required ESG "
        "report is not listed) and therefore cannot be accepted. The LTV 72% "
        "would be rejected against CP-CRE-2026-01 if the file were complete."
    )
    assert lead_decision_token(text) == "missing-data"
    assert expected_decision_token("rejected") == "reject"
    with pytest.raises(AssertionError, match="lead decision"):
        assert_expected_decision(text, "rejected")
    assert_expected_decision(text, "missing-data")
    assert_expected_decision("Judgement decision: reject. LTV 72%.", "rejected")


def test_load_judgement_cases_reads_manifest_outcomes(tmp_path: Path) -> None:
    base = tmp_path / "data" / "client-applications"
    base.mkdir(parents=True)
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "documents": {
                    "CA-20260115-1768478400000": {
                        "application_id": "CA-20260115-1768478400000",
                        "intended_outcome": "accepted",
                        "expected_outcome": "accepted",
                        "customer_name": "Helene Voss",
                    },
                    "CA-20260220-1771588800000": {
                        "intended_outcome": "rejected",
                        "expected_outcome": "rejected",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_judgement_cases(tmp_path) == [
        {
            "application_id": "CA-20260115-1768478400000",
            "intended_outcome": "accepted",
            "expected_outcome": "accepted",
        },
        {
            "application_id": "CA-20260220-1771588800000",
            "intended_outcome": "rejected",
            "expected_outcome": "rejected",
        },
    ]


def test_load_judgement_cases_missing_manifest(tmp_path: Path) -> None:
    assert load_judgement_cases(tmp_path) == []


def _labelled_cases(count: int) -> list[dict[str, str]]:
    return [
        {
            "application_id": f"CA-{index:04d}",
            "intended_outcome": "accepted",
            "expected_outcome": "accepted",
        }
        for index in range(count)
    ]


def test_latest_generated_cases_keeps_the_three_newest_ids() -> None:
    cases = _labelled_cases(6)
    chosen = latest_generated_cases(cases)
    assert [item["application_id"] for item in chosen] == [
        "CA-0003",
        "CA-0004",
        "CA-0005",
    ]
    short = latest_generated_cases(_labelled_cases(2))
    assert [item["application_id"] for item in short] == ["CA-0000", "CA-0001"]


def test_sample_judgement_cases_draws_five_and_keeps_the_seed() -> None:
    cases = _labelled_cases(12)
    order = [item["application_id"] for item in cases]
    first = sample_judgement_cases(cases, seed=0)
    assert sample_judgement_cases(cases, seed=0) == first
    assert len(first) == 5
    assert len({item["application_id"] for item in first}) == 5
    assert {item["application_id"] for item in first} < set(order)
    assert [item["application_id"] for item in first] == sorted(
        item["application_id"] for item in first
    )
    assert [item["application_id"] for item in cases] == order
    assert {
        item["application_id"] for item in sample_judgement_cases(cases, seed=1)
    } != {item["application_id"] for item in first}


def test_sample_judgement_cases_keeps_a_short_manifest() -> None:
    cases = _labelled_cases(3)
    chosen = sample_judgement_cases(cases, seed=4)
    assert chosen == cases
    assert chosen is not cases


def test_resolve_judgement_sample_seed_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2E_APPLICATION_SEED", "17")
    assert resolve_judgement_sample_seed() == 17
    monkeypatch.setenv("E2E_APPLICATION_SEED", "nope")
    assert resolve_judgement_sample_seed() == 0


def test_resolve_judgement_sample_seed_draws_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("E2E_APPLICATION_SEED", raising=False)

    class Fixed:
        def randrange(self, stop: int) -> int:
            assert stop == 1 << 31
            return 42

    monkeypatch.setattr("tests.live_support.random.SystemRandom", lambda: Fixed())
    assert resolve_judgement_sample_seed() == 42
    monkeypatch.setenv("E2E_APPLICATION_SEED", "  ")
    assert resolve_judgement_sample_seed() == 42


def test_assert_manifest_judgement_compares_received_decision() -> None:
    assert_manifest_judgement(
        "Judgement decision: reject. LTV 72%.",
        application_id="CA-20260220-1771588800000",
        intended_outcome="rejected",
        expected_outcome="rejected",
    )
    with pytest.raises(AssertionError, match="intended_outcome"):
        assert_manifest_judgement(
            "decision: reject",
            application_id="CA-20260220-1771588800000",
            intended_outcome="accepted",
            expected_outcome="rejected",
        )
    with pytest.raises(AssertionError, match="lead decision"):
        assert_manifest_judgement(
            "decision: accept",
            application_id="CA-20260220-1771588800000",
            intended_outcome="rejected",
            expected_outcome="rejected",
        )
