from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from google.adk.agents import Agent
from google.adk.tools import AgentTool, VertexAiSearchTool

CHAT_MODEL = "gemini-3.8-flash"
INSTRUCTIONS_RELATIVE = Path("agents/credit-policy-agent.instructions.md")
OUTPUTS_JSON_RELATIVE = Path("infra/outputs.json")
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


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def load_credit_officer_instructions(root: Path | None = None) -> str:
    path = (root or repo_root()) / INSTRUCTIONS_RELATIVE
    return path.read_text(encoding="utf-8")


def credit_officer_instruction(_ctx: object) -> str:
    # ADK injects {identifier} from session state on a string instruction.
    # The credit-officer file uses those braces as literal id shapes.
    return load_credit_officer_instructions()


def _output_value(spec: object) -> str:
    if isinstance(spec, dict) and "value" in spec:
        spec = spec["value"]
    if spec is None:
        return ""
    return str(spec).strip()


def data_store_from_outputs(root: Path | None = None) -> str:
    path = (root or repo_root()) / OUTPUTS_JSON_RELATIVE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return _output_value(data.get("DATA_STORE"))


def resolve_data_store_id(
    environ: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
) -> str:
    env = os.environ if environ is None else environ
    configured = env.get("DATA_STORE", "").strip()
    if configured:
        return configured
    return data_store_from_outputs(root)


def build_credit_officer(data_store_id: str) -> Agent:
    store = data_store_id.strip()
    if not store:
        raise ValueError("DATA_STORE is required")
    retrieval_agent = Agent(
        name="RetrievalAgent",
        model=CHAT_MODEL,
        description=RETRIEVAL_DESCRIPTION,
        instruction=RETRIEVAL_INSTRUCTION,
        tools=[VertexAiSearchTool(data_store_id=store)],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    return Agent(
        name="InteractiveAgent",
        model=CHAT_MODEL,
        description=INTERACTIVE_DESCRIPTION,
        instruction=credit_officer_instruction,
        tools=[
            AgentTool(
                agent=retrieval_agent,
                propagate_grounding_metadata=True,
            )
        ],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def load_root_agent(
    environ: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
) -> Agent:
    store = resolve_data_store_id(environ, root=root) or UNCONFIGURED_DATA_STORE
    return build_credit_officer(store)


root_agent = load_root_agent()
