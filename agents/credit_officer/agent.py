from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import AgentTool, VertexAiSearchTool

CHAT_MODEL = "gemini-3.8-flash"
INSTRUCTIONS_PATH = (
    Path(__file__).resolve().parent / "credit-policy-agent.instructions.md"
)
# Import must succeed when DATA_STORE is unset (unit tests, adk web).
# Agent Runtime injects the real id before process start. Search against
# this placeholder fails at query time; a blank id still fails in build.
UNCONFIGURED_DATA_STORE = (
    "projects/unset/locations/global/collections/default_collection/dataStores/unset"
)

RETRIEVAL_DESCRIPTION = (
    "Searches the one Agent Search data store of published credit policies "
    "and client applications. Returns passages with source_name or gs:// URIs."
)
RETRIEVAL_INSTRUCTION = (
    "Search the one Agent Search data store that holds the credit-policies "
    "prefix and the client-applications prefix. Return the retrieved passages. "
    "Include each document filename (source_name) or gs:// URI from the hit. "
    "Do not accept, reject, or mark missing-data. Do not invent thresholds. "
    "If the search returns no passages, say that the search returned no passages."
)
INTERACTIVE_DESCRIPTION = (
    "Contoso Demo Bank credit officer. Answers policy questions and evaluates "
    "applications by calling RetrievalAgent."
)


def chat_model() -> Gemini:
    # gemini-3.8-flash is published on global, us, and eu.
    return Gemini(
        model=CHAT_MODEL,
        client_kwargs={"vertexai": True, "location": "global"},
    )


def resolve_data_store_id(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("DATA_STORE", "").strip()


def build_credit_officer(data_store_id: str) -> Agent:
    store = data_store_id.strip()
    if not store:
        raise ValueError("DATA_STORE is required")
    retrieval_agent = Agent(
        name="RetrievalAgent",
        model=chat_model(),
        description=RETRIEVAL_DESCRIPTION,
        instruction=RETRIEVAL_INSTRUCTION,
        tools=[VertexAiSearchTool(data_store_id=store)],
        # Both flags and no sub-agents select SingleFlow. AutoFlow would
        # attach transfer handling.
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    # static_instruction skips {session_state} substitution. The file uses
    # braces as literal id shapes, such as {application_id}.
    return Agent(
        name="InteractiveAgent",
        model=chat_model(),
        description=INTERACTIVE_DESCRIPTION,
        static_instruction=INSTRUCTIONS_PATH.read_text(encoding="utf-8"),
        tools=[
            AgentTool(
                agent=retrieval_agent,
                propagate_grounding_metadata=True,
            )
        ],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def load_root_agent(environ: Mapping[str, str] | None = None) -> Agent:
    store = resolve_data_store_id(environ) or UNCONFIGURED_DATA_STORE
    return build_credit_officer(store)


def _reasoning_engine_app():
    # Agent Runtime registers query methods on the entrypoint. An LlmAgent has
    # none of them; AdkApp is the object the runtime knows how to serve.
    # enable_tracing records the model request, the reply, and tool results
    # on Cloud Trace spans. AdkApp omits those attributes unless this is set.
    agent = load_root_agent()
    try:
        from vertexai.agent_engines import AdkApp
    except ImportError:
        return agent
    return AdkApp(agent=agent, enable_tracing=True)


root_agent = _reasoning_engine_app()
