from __future__ import annotations

import pytest

from tests.live_support import (
    assert_manifest_judgement,
    invoke_agent,
    latest_generated_cases,
    load_judgement_cases,
    require_manifest_outcomes,
)

pytestmark = [pytest.mark.agent, pytest.mark.timeout(600)]


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
    ids = [case["application_id"] for case in chosen]
    if "agent" in (metafunc.config.option.markexpr or ""):
        print(
            f"\nAgent judgement last {len(chosen)} generated of {len(cases)}: "
            f"{', '.join(ids)}",
            flush=True,
        )
    metafunc.parametrize(
        "judgement_case",
        chosen,
        ids=ids,
    )


def test_agent_judgement_matches_manifest(
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
