from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.adk.tools import AgentTool, VertexAiSearchTool

from agents.credit_officer.agent import (
    CHAT_MODEL,
    UNCONFIGURED_DATA_STORE,
    build_credit_officer,
    credit_officer_instruction,
    load_credit_officer_instructions,
    load_root_agent,
    resolve_data_store_id,
)
from talos.constants import DEFAULT_CHAT_MODEL
from talos.env import repo_root

pytestmark = pytest.mark.unit

STORE = (
    "projects/lab5-gemini-dev1/locations/global/"
    "collections/default_collection/dataStores/kb-credit-policies"
)
OTHER_STORE = (
    "projects/lab5-gemini-dev1/locations/global/"
    "collections/default_collection/dataStores/from-outputs"
)


def _officer_text() -> str:
    return credit_officer_instruction(None)


def test_v1_instructions_are_grounded_only() -> None:
    text = _officer_text()
    assert text == load_credit_officer_instructions()
    assert "That is not in the published policies." in text
    assert (
        "Never infer outcome from `application_id`, filename, or `source_name`." in text
    )
    assert "application_id" in text
    assert "customer_name" in text
    assert "expected_outcome" in text


def test_v3_instructions_require_citations() -> None:
    text = _officer_text()
    assert "source_name" in text
    assert "gs://" in text
    assert "sole source of a policy threshold" in text
    assert "credit-application-{application_id}.md" in text


def test_v4_retrieval_agent_is_vertex_ai_search_only(tmp_path: Path) -> None:
    outputs = tmp_path / "infra"
    outputs.mkdir()
    (outputs / "outputs.json").write_text(
        json.dumps({"DATA_STORE": {"value": OTHER_STORE}}),
        encoding="utf-8",
    )
    assert resolve_data_store_id({}, root=tmp_path) == OTHER_STORE
    assert resolve_data_store_id({"DATA_STORE": STORE}, root=tmp_path) == STORE
    assert resolve_data_store_id({}, root=tmp_path / "missing") == ""

    officer = build_credit_officer(STORE)
    assert officer.name == "InteractiveAgent"
    assert officer.sub_agents == []
    assert len(officer.tools) == 1
    wrapped = officer.tools[0]
    assert isinstance(wrapped, AgentTool)
    retrieval = wrapped.agent
    assert retrieval.name == "RetrievalAgent"
    assert retrieval.sub_agents == []
    assert len(retrieval.tools) == 1
    search = retrieval.tools[0]
    assert isinstance(search, VertexAiSearchTool)
    assert search.data_store_id == STORE
    assert search.search_engine_id is None

    from_env = load_root_agent({"DATA_STORE": STORE}, root=tmp_path)
    assert from_env.tools[0].agent.tools[0].data_store_id == STORE
    from_outputs = load_root_agent({}, root=tmp_path)
    assert from_outputs.tools[0].agent.tools[0].data_store_id == OTHER_STORE
    unset = load_root_agent({}, root=tmp_path / "missing")
    assert unset.tools[0].agent.tools[0].data_store_id == UNCONFIGURED_DATA_STORE
    with pytest.raises(ValueError, match="DATA_STORE"):
        build_credit_officer("  ")


def test_v7_chat_model_does_not_set_embeddings() -> None:
    officer = build_credit_officer(STORE)
    retrieval = officer.tools[0].agent
    assert officer.model == CHAT_MODEL == DEFAULT_CHAT_MODEL == "gemini-3.8-flash"
    assert retrieval.model == CHAT_MODEL
    package = repo_root() / "agents" / "credit_officer"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))
    )
    assert "text-embedding-005" not in source
