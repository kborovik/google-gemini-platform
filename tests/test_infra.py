from __future__ import annotations

import pytest

from talos.env import repo_root

pytestmark = pytest.mark.unit


def test_tfvars_pin_lab5_gemini_dev1() -> None:
    text = (repo_root() / "infra/lab5-gemini-dev1.tfvars").read_text(encoding="utf-8")
    assert 'project = "lab5-gemini-dev1"' in text
    assert 'region  = "us-east1"' in text


def test_makefile_uses_org_factory_state_bucket() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "PROJECT ?= lab5-gemini-dev1" in text
    assert "TFSTATE_BUCKET := terraform-$(PROJECT)" in text
    assert "infra-backend-destroy" not in text
