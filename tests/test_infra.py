from __future__ import annotations

import re

import pytest

from talos.env import repo_root

pytestmark = pytest.mark.unit


def test_rag_engine_tier_is_outside_the_allowlist() -> None:
    text = (repo_root() / "infra/rag.tf").read_text(encoding="utf-8")
    assert 'region  = "us-east5"' in text


def test_tfvars_pin_lab5_gemini_dev1() -> None:
    text = (repo_root() / "infra/lab5-gemini-dev1.tfvars").read_text(encoding="utf-8")
    assert 'project = "lab5-gemini-dev1"' in text
    assert 'region  = "us-east1"' in text


def test_makefile_rag_destroy_targets_only_the_engine() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    match = re.search(
        r"^infra-rag-destroy:[^\n]*\n((?:[ \t].*\n)*)",
        text,
        re.M,
    )
    assert match is not None
    body = match.group(1)
    assert "terraform -chdir=infra destroy" in body
    assert "-target=google_vertex_ai_rag_engine_config.basic" in body
    assert "outputs.json" not in body


def test_makefile_uses_org_factory_state_bucket() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "PROJECT ?= lab5-gemini-dev1" in text
    assert "TFSTATE_BUCKET := terraform-$(PROJECT)" in text
    assert "infra-backend-create" not in text
    assert "infra-backend-destroy" not in text
