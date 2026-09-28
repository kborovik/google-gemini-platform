from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.tools import FunctionTool
from google.genai import types

from agents.credit_officer import agent as credit_officer
from agents.credit_officer.agent import (
    CHAT_MODEL,
    CLIENT_APPLICATIONS_CORPUS,
    CONTEXT_CACHE_TTL_SECONDS,
    CORPUS_FILTER,
    GEMINI_3_MIN_CACHE_TOKENS,
    INSTRUCTIONS_PATH,
    NO_MATCH_SENTENCE,
    NO_SEARCH_SENTENCE,
    UNCONFIGURED_DATA_STORE,
    ApplicationHit,
    PolicyPackCache,
    build_credit_officer,
    build_credit_officer_app,
    format_lookup_hits,
    load_root_agent,
    parse_application_hits,
    gcs_bearer_headers,
    lookup_query,
    resolve_data_store_id,
    search_client_applications,
)
from docgen.constants import CLIENT_APPLICATIONS_CORPUS as INDEX_CORPUS
from docgen.constants import DEFAULT_CHAT_MODEL
from docgen.deploy import render_policy_pack
from docgen.env import repo_root

pytestmark = pytest.mark.unit

STORE = (
    "projects/lab5-gemini-dev1/locations/global/"
    "collections/default_collection/dataStores/kb-credit-policies"
)


def _officer_text() -> str:
    text = build_credit_officer(STORE).static_instruction
    assert isinstance(text, str)
    return text


def test_v1_instructions_are_grounded_only() -> None:
    officer = build_credit_officer(STORE)
    text = officer.static_instruction
    instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    assert isinstance(text, str)
    assert text.startswith(instructions.rstrip())
    assert "\n\n# PolicyPack\n" in text
    assert officer.instruction == ""
    assert INSTRUCTIONS_PATH.is_file()
    assert INSTRUCTIONS_PATH.parent == Path(credit_officer.__file__).resolve().parent
    assert "That is not in the published policies." in text
    assert (
        "Never infer outcome from `application_id`, filename, or `source_name`." in text
    )
    assert "application_id" in text
    assert "customer_name" in text
    assert "expected_outcome" in text
    assert NO_MATCH_SENTENCE in text


def test_lookup_query_treats_json_null_as_empty() -> None:
    assert lookup_query("CA-1", None) == "CA-1"
    assert lookup_query(None, "Ada Lovelace") == "Ada Lovelace"
    assert lookup_query(None, None) is None
    calls: list[str] = []

    def searcher(query: str) -> list[ApplicationHit]:
        calls.append(query)
        return []

    officer = build_credit_officer(STORE, searcher=searcher)
    tool = officer.tools[0]
    assert isinstance(tool, FunctionTool)
    tool.func(application_id="CA-1", customer_name=None)
    assert calls == ["CA-1"]


def test_v1_lookup_does_not_judge_a_miss_or_several_hits() -> None:
    assert format_lookup_hits([]) == NO_MATCH_SENTENCE
    for word in ("accepted", "rejected", "missing-data"):
        assert word not in NO_MATCH_SENTENCE
    several = format_lookup_hits(
        [
            ApplicationHit("credit-application-CA-1.md", "gs://b/a.md", "CA-1", "one"),
            ApplicationHit("credit-application-CA-2.md", "gs://b/b.md", "CA-2", "two"),
        ]
    )
    assert "CA-1" in several and "CA-2" in several
    assert "Do not judge." in several
    for word in ("accepted", "rejected", "missing-data"):
        assert word not in several
    one = format_lookup_hits(
        [
            ApplicationHit(
                "credit-application-CA-9.md",
                "gs://b/client-applications/credit-application-CA-9.md",
                "CA-9",
                "ltv: 71%",
            )
        ]
    )
    for word in ("accepted", "rejected", "missing-data"):
        assert word not in one


def test_v3_instructions_require_citations() -> None:
    text = _officer_text()
    assert "source_name" in text
    assert "gs://" in text
    assert "sole source of a policy threshold" in text
    assert "credit-application-{application_id}.md" in text
    hit = format_lookup_hits(
        [
            ApplicationHit(
                "credit-application-CA-1.md",
                "gs://bucket/client-applications/credit-application-CA-1.md",
                "CA-1",
                "ltv: 71%",
            )
        ]
    )
    assert "source_name: credit-application-CA-1.md" in hit
    assert "gs://bucket/client-applications/credit-application-CA-1.md" in hit


def test_v4_only_interactive_agent_looks_up_one_application() -> None:
    assert resolve_data_store_id({}) == ""
    assert resolve_data_store_id({"DATA_STORE": STORE}) == STORE
    assert CLIENT_APPLICATIONS_CORPUS == INDEX_CORPUS == "client-applications"

    calls: list[str] = []

    def searcher(query: str) -> list[ApplicationHit]:
        calls.append(query)
        return [
            ApplicationHit(
                "credit-application-CA-1.md",
                "gs://b/credit-application-CA-1.md",
                "CA-1",
                "filing",
            )
        ]

    officer = build_credit_officer(STORE, searcher=searcher)
    assert officer.name == "InteractiveAgent"
    assert officer.sub_agents == []
    assert len(officer.tools) == 1
    tool = officer.tools[0]
    assert isinstance(tool, FunctionTool)
    assert tool.name == "lookup_application"
    assert calls == []
    assert tool.func(application_id="CA-1")
    assert tool.func(customer_name="Helene Voss")
    assert calls == ["CA-1", "Helene Voss"]
    assert (
        tool.func(application_id="CA-1", customer_name="Helene Voss")
        == NO_SEARCH_SENTENCE
    )
    assert tool.func() == NO_SEARCH_SENTENCE
    assert tool.func(application_id="  ", customer_name="  ") == NO_SEARCH_SENTENCE
    assert calls == ["CA-1", "Helene Voss"]
    assert tool.func.data_store_id == STORE

    from_env = load_root_agent({"DATA_STORE": STORE})
    assert from_env.tools[0].func.data_store_id == STORE
    unset = load_root_agent({})
    assert unset.tools[0].func.data_store_id == UNCONFIGURED_DATA_STORE
    with pytest.raises(ValueError, match="DATA_STORE"):
        build_credit_officer("  ")

    package = repo_root() / "agents" / "credit_officer"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))
    )
    assert "RetrievalAgent" not in source
    assert "AgentTool" not in source
    assert "VertexAiSearchTool" not in source
    static = officer.static_instruction
    assert isinstance(static, str)
    assert "Do not call lookup_application for a policy question." in static
    assert "Call lookup_application once" in static
    copy.deepcopy(officer)


def test_v4_gcs_read_does_not_send_a_quota_project() -> None:
    headers = gcs_bearer_headers("token")
    assert headers == {"Authorization": "Bearer token"}
    assert "x-goog-user-project" not in headers


def test_v4_search_requests_only_client_applications() -> None:
    posts: list[tuple[str, dict[str, object]]] = []

    def post(url: str, body: dict[str, object]) -> dict[str, object]:
        posts.append((url, body))
        return {
            "results": [
                {
                    "document": {
                        "derivedStructData": {
                            "link": (
                                "gs://b/client-applications/credit-application-CA-1.md"
                            ),
                            "extractive_segments": [{"content": "ltv: 71%"}],
                        }
                    }
                }
            ]
        }

    hits = search_client_applications(STORE, "CA-1", post=post)
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/servingConfigs/default_search:search")
    assert STORE in url
    assert body["query"] == '"CA-1"'
    assert body["filter"] == CORPUS_FILTER
    assert "client-applications" in str(body["filter"])
    assert "credit-policies" not in str(body["filter"])
    spec = body["contentSearchSpec"]
    assert isinstance(spec, dict)
    assert spec["snippetSpec"] == {"returnSnippet": True, "maxSnippetCount": 5}
    assert "extractiveContentSpec" not in spec
    assert hits[0].application_id == "CA-1"
    assert hits[0].text == "ltv: 71%"
    parsed = parse_application_hits(
        {
            "results": [
                {"document": {"derivedStructData": {"link": "gs://b/a.md"}}},
                {"document": {"derivedStructData": {"link": "gs://b/a.md"}}},
            ]
        }
    )
    assert len(parsed) == 1

    reads: list[str] = []

    def reader(uri: str) -> str:
        reads.append(uri)
        return "# Credit application CA-1\ncustomer_name: Helene Voss\nltv: 71%\n"

    officer = build_credit_officer(
        STORE, searcher=lambda query: hits, object_reader=reader
    )
    tool = officer.tools[0]
    assert isinstance(tool, FunctionTool)
    text = tool.func(application_id="CA-1")
    assert reads == [hits[0].uri]
    assert "customer_name: Helene Voss" in text
    assert "ltv: 71%" in text
    missing = build_credit_officer(
        STORE,
        searcher=lambda query: hits,
        object_reader=lambda uri: (_ for _ in ()).throw(OSError(uri)),
    )
    assert missing.tools[0].func(application_id="CA-1") == NO_MATCH_SENTENCE


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_v4_policy_pack_is_one_read_per_cache_fill() -> None:
    payload, digest = render_policy_pack(repo_root() / "data" / "credit-policies")
    state = {"body": payload.decode("utf-8"), "sha": digest, "fail": False, "reads": 0}
    uri = "gs://lab5-gemini-dev1-credit-docs/credit-policies/policy-pack.md"

    def reader(requested: str) -> tuple[str, str]:
        assert requested == uri
        state["reads"] += 1
        if state["fail"]:
            raise OSError("cache miss")
        return str(state["body"]), str(state["sha"])

    clock = _Clock()
    pack = PolicyPackCache(
        uri=uri,
        ttl_seconds=CONTEXT_CACHE_TTL_SECONDS,
        reader=reader,
        clock=clock,
    )
    officer = build_credit_officer(STORE, pack=pack, searcher=lambda _query: [])
    assert state["reads"] == 1
    assert payload.decode("utf-8") in officer.static_instruction
    request = SimpleNamespace(
        config=types.GenerateContentConfig(system_instruction="stale")
    )
    officer.before_model_callback(None, request)
    assert state["reads"] == 1
    assert request.config.system_instruction == "stale"

    clock.now = CONTEXT_CACHE_TTL_SECONDS
    state["body"] = "same-bytes-ignored"
    state["sha"] = digest
    officer.before_model_callback(None, request)
    assert state["reads"] == 2
    assert "same-bytes-ignored" not in officer.static_instruction
    assert payload.decode("utf-8") in officer.static_instruction

    clock.now = CONTEXT_CACHE_TTL_SECONDS * 2
    state["body"] = "pack-v2\n"
    state["sha"] = "b" * 64
    officer.before_model_callback(None, request)
    assert state["reads"] == 3
    assert officer.static_instruction.endswith("pack-v2\n")
    assert request.config.system_instruction == officer.static_instruction

    clock.now = CONTEXT_CACHE_TTL_SECONDS * 3
    state["fail"] = True
    officer.before_model_callback(None, request)
    assert state["reads"] == 4
    assert officer.static_instruction.endswith("pack-v2\n")


def test_v4_context_cache_uses_the_model_minimum() -> None:
    app = build_credit_officer_app(STORE, searcher=lambda _query: [])
    assert app.root_agent.name == "InteractiveAgent"
    assert app.root_agent.sub_agents == []
    config = app.context_cache_config
    assert config is not None
    assert config.min_tokens == GEMINI_3_MIN_CACHE_TOKENS == 4096
    assert config.ttl_seconds == CONTEXT_CACHE_TTL_SECONDS


def test_v7_chat_model_does_not_set_embeddings() -> None:
    officer = build_credit_officer(STORE)
    assert officer.model.model == CHAT_MODEL == DEFAULT_CHAT_MODEL == "gemini-3.8-flash"
    assert officer.model.client_kwargs == {"vertexai": True, "location": "global"}
    package = repo_root() / "agents" / "credit_officer"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))
    )
    assert "outputs.json" not in source
    assert "pyproject.toml" not in source
    assert "AdkApp(app=app, enable_tracing=True)" in source
    assert "object_reader=read_gcs_text" in source
