from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from docgen.env import repo_root

pytestmark = pytest.mark.unit


def _pause():
    path = repo_root() / "scripts" / "pause_idle.py"
    spec = importlib.util.spec_from_file_location("pause_idle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tier_of_reads_spanner_and_serverless_shapes() -> None:
    pause = _pause()
    assert (
        pause.tier_of({"ragManagedDbConfig": {"basic": {}, "spanner": {"basic": {}}}})
        == "basic"
    )
    assert (
        pause.tier_of(
            {
                "ragManagedDbConfig": {
                    "unprovisioned": {},
                    "spanner": {"unprovisioned": {}},
                }
            }
        )
        == "unprovisioned"
    )
    assert pause.tier_of({"ragManagedDbConfig": {"serverless": {}}}) == "serverless"
    assert (
        pause.tier_of({"ragManagedDbConfig": {"scaled": {}, "spanner": {"scaled": {}}}})
        == "scaled"
    )
    assert pause.tier_of({}) == "unknown"


def test_action_for_patches_empty_spanner_tiers_only() -> None:
    pause = _pause()
    assert pause.action_for("basic", 0) == "patch"
    assert pause.action_for("scaled", 0) == "patch"
    assert pause.action_for("unprovisioned", None) == "skip"
    assert pause.action_for("serverless", None) == "skip"
    with pytest.raises(ValueError, match="corpora"):
        pause.action_for("basic", 2)
    with pytest.raises(ValueError, match="unrecognized"):
        pause.action_for("unknown", 0)


def test_default_regions_are_the_idle_basic_tiers() -> None:
    pause = _pause()
    assert pause.DEFAULT_REGIONS[0] == "us-west1"
    assert "us-east1" not in pause.DEFAULT_REGIONS
    assert "us-east5" not in pause.DEFAULT_REGIONS
    assert "us-central1" not in pause.DEFAULT_REGIONS


def test_makefile_pause_stops_idle_rag_and_scales_chat() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^pause: prompt\b", text, re.M)
    assert "infra-rag-destroy" not in text
    match = re.search(r"^pause:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert match is not None
    body = match.group(1)
    assert "scripts/pause_idle.py" in body
    assert "RAG_IDLE_REGIONS" in body
    assert "--min-instances=0" in body
    assert "services update chat" in body
    assert "apply -destroy" not in body
    assert "reasoningEngines" not in body
    assert "google_vertex_ai_rag_engine_config" not in text
    assert (Path(repo_root() / "scripts" / "pause_idle.py")).is_file()
