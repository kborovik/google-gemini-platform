"""Live Chat agent judgements.

`gmake e2e` selects the judgement tests with `-m agent`.
`-m teams` selects those judgements and the no-id help check.
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.live_support import (
    ACK_TEXT,
    CHAT_SURFACE,
    assert_manifest_judgement,
    invoke_agent,
    latest_generated_cases,
    load_judgement_cases,
    post_chat_message,
    read_follow_up,
    require_chat_thread,
    require_manifest_outcomes,
)

pytestmark = [pytest.mark.teams, pytest.mark.timeout(600)]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "judgement_case" not in metafunc.fixturenames:
        return
    cases = load_judgement_cases()
    if not cases:
        metafunc.parametrize(
            "judgement_case",
            [
                pytest.param(
                    None,
                    marks=pytest.mark.skip(
                        reason="no data/client-applications/manifest.json"
                    ),
                )
            ],
        )
        return
    chosen = latest_generated_cases(cases)
    markexpr = metafunc.config.option.markexpr or ""
    if "agent" in markexpr or "teams" in markexpr:
        filing_ids = [case["application_id"] for case in chosen]
        print(
            f"\nChat agent judgement last {len(chosen)} generated of {len(cases)} "
            f"on {CHAT_SURFACE}: {', '.join(filing_ids)}",
            flush=True,
        )
    metafunc.parametrize(
        "judgement_case",
        chosen,
        ids=[case["application_id"] for case in chosen],
    )


@pytest.mark.agent
def test_chat_agent_judgement_matches_manifest(
    live_env: dict[str, str],
    judgement_case: dict[str, str] | None,
) -> None:
    assert judgement_case is not None
    application_id = judgement_case["application_id"]
    intended_outcome = judgement_case["intended_outcome"]
    expected_outcome = judgement_case["expected_outcome"]
    require_manifest_outcomes(
        application_id=application_id,
        intended_outcome=intended_outcome,
        expected_outcome=expected_outcome,
    )
    text = invoke_agent(
        live_env,
        "Evaluate client application "
        f"{application_id} against published credit policy.",
    )
    assert_manifest_judgement(
        text,
        application_id=application_id,
        intended_outcome=intended_outcome,
        expected_outcome=expected_outcome,
    )


def test_message_ack_then_reads_the_follow_up(live_env: dict[str, str]) -> None:
    if not live_env.get("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("GOOGLE_CLOUD_PROJECT is not set")
    space, thread = require_chat_thread(live_env)
    question = "What is the max LTV for an owner-occupied residential mortgage?"
    started = time.time()
    reply = post_chat_message(
        question,
        thread=thread,
        space=space,
        user="users/live",
        message_name=f"{thread}/messages/{uuid.uuid4().hex}",
    )
    assert reply["thread"] == {"name": thread}
    assert reply["text"] == ACK_TEXT
    follow_up = read_follow_up(space=space, thread=thread, not_before=started)
    assert follow_up != ACK_TEXT
    assert "CA-{YYYYMMDD}-{unix_ms}" in follow_up
    assert "CA-20260115-1736899200123" in follow_up
