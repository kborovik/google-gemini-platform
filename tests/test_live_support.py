from __future__ import annotations

import pytest

from tests.live_support import (
    application_cases,
    assert_expected_decision,
    expected_decision_token,
    flatten_retrieve_text,
    lead_decision_token,
    pick_application_case,
    print_agent_turn,
    search_request,
)

pytestmark = pytest.mark.unit


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
