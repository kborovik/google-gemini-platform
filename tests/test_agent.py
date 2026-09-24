from __future__ import annotations

import pytest

from tests.live_support import (
    agent_judgement_runs,
    assert_manifest_judgement,
    invoke_on_surface,
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
            "agent_surface,judgement_case",
            [
                pytest.param(
                    "google_cloud_run_v2_service.chat",
                    None,
                    marks=pytest.mark.skip(
                        reason="no data/client-applications/manifest.json"
                    ),
                )
            ],
        )
        return
    chosen = latest_generated_cases(cases)
    runs = agent_judgement_runs(chosen)
    ids = [f"{surface}-{case['application_id']}" for surface, case in runs]
    if "agent" in (metafunc.config.option.markexpr or ""):
        filing_ids = [case["application_id"] for case in chosen]
        print(
            f"\nAgent judgement last {len(chosen)} generated of {len(cases)} "
            f"on the reasoning engine, then chat: {', '.join(filing_ids)}",
            flush=True,
        )
    metafunc.parametrize(
        "agent_surface,judgement_case",
        runs,
        ids=ids,
    )


def test_agent_judgement_matches_manifest(
    live_env: dict[str, str],
    agent_surface: str,
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
    text = invoke_on_surface(
        live_env,
        "Evaluate client application "
        f"{application_id} against published credit policy.",
        agent_surface,
    )
    assert_manifest_judgement(
        text,
        application_id=application_id,
        intended_outcome=intended_outcome,
        expected_outcome=expected_outcome,
    )
