from __future__ import annotations

import pytest

from docgen.constants import DEFAULT_INSTRUCTIONS_RELATIVE, REFUSAL_SENTENCE, WATERMARK
from docgen.env import repo_root

pytestmark = pytest.mark.unit


def test_instructions_contain_evaluation_mode() -> None:
    text = (repo_root() / DEFAULT_INSTRUCTIONS_RELATIVE).read_text(encoding="utf-8")
    upper = text.upper()
    assert "EVALUATION MODE" in upper or "EVALUATIONMODE" in upper.replace(" ", "")
    assert "application_id" in text
    assert "customer_name" in text
    assert "ask" in text.lower()
    assert "missing" in text.lower()
    assert "accept" in text.lower() and "reject" in text.lower()
    assert "missing-data" in text
    assert "client-applications" in text
    assert "credit-policies" in text
    assert "source_name" in text
    assert "gs://" in text
    assert "never cite an application blob" not in text.lower()
    assert "credit-application-{application_id}.md" in text
    assert "sole source" in text.lower()
    assert "application facts" in text.lower()
    assert "policy thresholds" in text.lower()
    assert (
        REFUSAL_SENTENCE in text.lower()
        or "That is not in the published policies." in text
    )
    assert WATERMARK not in text
    assert "synthetic demo corpus" not in text.lower()
    assert "type nicknames" in text.lower() or "`accepted`" in text
    assert "CA-{YYYYMMDD}-{unix_ms}" in text
    assert "CP-DOC-2026-01" in text
    assert "attached" in text.lower()
    assert "required title" in text.lower()
    assert "appraisal_date" in text.lower() or "appraisal date" in text.lower()
    assert "expected_outcome" in text
    assert "infer" in text.lower()
    assert "filename" in text.lower()
    assert "judgement" in text.lower()
