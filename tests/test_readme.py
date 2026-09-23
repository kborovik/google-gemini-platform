from __future__ import annotations

import re

import pytest

from docgen.env import repo_root

pytestmark = pytest.mark.unit

_RAG_WORD = re.compile(r"\brag\b", re.IGNORECASE)


def test_readme_is_hiring_manager_not_runbook() -> None:
    text = (repo_root() / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 105
    assert "sequenceDiagram" in text
    lowered = text.lower()
    assert "talos" not in lowered
    assert _RAG_WORD.search(text) is None
    assert "```bash" not in text
    assert "gmake " not in text
    assert "uv run" not in text
    assert "adk deploy" not in text
    for phrase in ("Google Chat", "Agent Search", "Agent Runtime", "docs/demo.md"):
        assert phrase in text


def test_demo_path_is_chat_and_agent_search() -> None:
    text = (repo_root() / "docs/demo.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "talos" not in lowered
    assert "rag corpus" not in lowered
    assert "rag engine" not in lowered
    assert "adk deploy" not in lowered
    for phrase in (
        "gmake generate",
        "uv run docgen generate application --all --local-only",
        "gmake deploy",
        "docgen upload",
        "docgen index --wait",
        "agents/credit_officer",
        "credit-officer",
        "credit-policy-agent",
        "kb-credit-policies",
        "projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/{id}",
        "DATA_STORE",
        "REASONING_ENGINE",
        "chat/main.py",
        "https://chat.lab5.ca",
        "Cloudflare DNS-only CNAME",
        "ghs.googlehosted.com",
        "gmake chat-deploy",
        "Google Chat",
        "async_stream_query",
    ):
        assert phrase in text
    assert "python -m docgen.google_chat" not in text
    assert "docgen.google_chat" not in text
