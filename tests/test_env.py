from __future__ import annotations

import json
from pathlib import Path

import pytest

from docgen.env import fill_missing, parse_terraform_output, resolve_env

pytestmark = pytest.mark.unit


def test_parse_terraform_output_unwraps_value() -> None:
    parsed = parse_terraform_output(
        json.dumps(
            {
                "GOOGLE_CLOUD_PROJECT": {"value": "lab5-gemini-dev1"},
                "GCS_BUCKET": {"value": "lab5-gemini-dev1-credit-docs"},
                "EMPTY": {"value": ""},
            }
        )
    )
    assert parsed["GOOGLE_CLOUD_PROJECT"] == "lab5-gemini-dev1"
    assert "EMPTY" not in parsed


def test_fill_missing_does_not_overwrite() -> None:
    env = {"GOOGLE_CLOUD_PROJECT": "already"}
    fill_missing(env, {"GOOGLE_CLOUD_PROJECT": "from-file", "GCS_BUCKET": "bucket"})
    assert env["GOOGLE_CLOUD_PROJECT"] == "already"
    assert env["GCS_BUCKET"] == "bucket"


def test_no_terraform_ignores_outputs_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.setattr(
        "docgen.env.repo_root",
        lambda: tmp_path,
    )
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "outputs.json").write_text(
        json.dumps({"GCS_BUCKET": {"value": "from-file"}}),
        encoding="utf-8",
    )
    env = resolve_env(use_terraform=False)
    assert "GCS_BUCKET" not in env or env.get("GCS_BUCKET") != "from-file"
