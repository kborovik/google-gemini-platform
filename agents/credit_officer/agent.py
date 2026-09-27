from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App
from google.adk.models import Gemini
from google.adk.tools import FunctionTool

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
# Gemini 3 explicit context-cache floor. ADK also applies this minimum.
GEMINI_3_MIN_CACHE_TOKENS = 4096
# Same default as ADK ContextCacheConfig.ttl_seconds. One GCS read per fill.
CONTEXT_CACHE_TTL_SECONDS = 1800
CLIENT_APPLICATIONS_CORPUS = "client-applications"
CORPUS_FILTER = 'corpus: ANY("client-applications")'
NO_MATCH_SENTENCE = "No application matched that id or name."
NO_SEARCH_SENTENCE = "No search and no judgement."
POLICY_PACK_HEADING = "# PolicyPack"
INTERACTIVE_DESCRIPTION = (
    "Contoso Demo Bank credit officer. Answers policy questions from PolicyPack "
    "and evaluates one application with lookup_application."
)
_APPLICATION_PREFIX = "credit-application-"
_SEARCH_PAGE_SIZE = 10

PackReader = Callable[[str], tuple[str, str]]
Clock = Callable[[], float]
Poster = Callable[[str, dict[str, Any]], dict[str, Any]]
Searcher = Callable[[str], list["ApplicationHit"]]


@dataclass(frozen=True)
class ApplicationHit:
    source_name: str
    uri: str
    application_id: str
    text: str


class PolicyPackCache:
    """One read of POLICY_PACK_URI per cache fill.

    Process start and context-cache expiry each call fill. The fill compares
    content_sha256. A new hash replaces the pack. A failed read keeps the
    last fill.
    """

    def __init__(
        self,
        uri: str,
        *,
        ttl_seconds: float = CONTEXT_CACHE_TTL_SECONDS,
        reader: PackReader | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.uri = uri.strip()
        self.ttl_seconds = ttl_seconds
        self._reader = reader or read_policy_pack
        self._clock = clock or time.monotonic
        self.text = ""
        self.sha256 = ""
        self._filled_at: float | None = None

    def fill(self) -> str:
        if not self.uri:
            return self.text
        now = self._clock()
        try:
            text, digest = self._reader(self.uri)
        except Exception:
            self._filled_at = now
            return self.text
        digest = digest.strip().lower()
        if digest != self.sha256:
            self.text = text
            self.sha256 = digest
        self._filled_at = now
        return self.text

    def current(self) -> str:
        if not self.uri:
            return self.text
        now = self._clock()
        if (
            self._filled_at is not None
            and now - self._filled_at < self.ttl_seconds
        ):
            return self.text
        return self.fill()


def compose_static_instruction(instructions: str, pack: str) -> str:
    return f"{instructions.rstrip()}\n\n{POLICY_PACK_HEADING}\n{pack}"


def chat_model() -> Gemini:
    # gemini-3.8-flash is published on global, us, and eu.
    return Gemini(
        model=CHAT_MODEL,
        client_kwargs={"vertexai": True, "location": "global"},
    )


def resolve_data_store_id(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("DATA_STORE", "").strip()


def split_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs URI: {uri}")
    rest = uri.removeprefix("gs://")
    bucket, _, name = rest.partition("/")
    if not bucket or not name:
        raise ValueError(f"not a gs URI: {uri}")
    return bucket, name


def read_policy_pack(uri: str) -> tuple[str, str]:
    """One object read. Hash is metadata content_sha256, else the UTF-8 bytes."""
    bucket_name, object_name = split_gs_uri(uri)
    from google.cloud import storage

    blob = storage.Client().bucket(bucket_name).blob(object_name)
    blob.reload()
    data = blob.download_as_bytes()
    metadata = blob.metadata or {}
    digest = str(metadata.get("content_sha256") or "").strip().lower()
    if not digest:
        digest = hashlib.sha256(data).hexdigest()
    return data.decode("utf-8"), digest


def lookup_query(application_id: str, customer_name: str) -> str | None:
    """Exactly one identifier. Both empty or both set means no search."""
    application = application_id.strip()
    customer = customer_name.strip()
    if bool(application) == bool(customer):
        return None
    return application or customer


def application_id_from_source(source_name: str) -> str:
    name = source_name.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    if not name.startswith(_APPLICATION_PREFIX):
        return ""
    return name[len(_APPLICATION_PREFIX) :]


def parse_application_hits(payload: dict[str, Any]) -> list[ApplicationHit]:
    hits: list[ApplicationHit] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        document = item.get("document")
        if not isinstance(document, dict):
            document = {}
        derived = document.get("derivedStructData")
        if not isinstance(derived, dict):
            derived = {}
        content = document.get("content")
        if not isinstance(content, dict):
            content = {}
        uri = ""
        for candidate in (derived.get("link"), derived.get("uri"), content.get("uri")):
            if isinstance(candidate, str) and candidate:
                uri = candidate
                break
        source_name = uri.rsplit("/", 1)[-1] if uri else ""
        key = uri or source_name
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        hits.append(
            ApplicationHit(
                source_name=source_name,
                uri=uri,
                application_id=application_id_from_source(source_name),
                text=_hit_text(derived),
            )
        )
    return hits


def format_lookup_hits(hits: list[ApplicationHit]) -> str:
    if not hits:
        return NO_MATCH_SENTENCE
    if len(hits) > 1:
        ids = [hit.application_id for hit in hits if hit.application_id]
        listed = ", ".join(ids) if ids else ", ".join(
            hit.source_name for hit in hits if hit.source_name
        )
        return f"Matching application_id values: {listed}. Do not judge."
    hit = hits[0]
    lines = [f"source_name: {hit.source_name}", hit.uri, hit.text]
    return "\n".join(line for line in lines if line)


def search_client_applications(
    data_store_id: str,
    query: str,
    *,
    post: Poster | None = None,
) -> list[ApplicationHit]:
    """Exactly one Agent Search request, filtered to client-applications."""
    send = post or _post_discovery
    body = {
        "query": query,
        "filter": CORPUS_FILTER,
        "pageSize": _SEARCH_PAGE_SIZE,
        "contentSearchSpec": {
            "snippetSpec": {"returnSnippet": True, "maxSnippetCount": 5},
            "extractiveContentSpec": {"maxExtractiveSegmentCount": 1},
        },
    }
    payload = send(_search_url(data_store_id), body)
    return parse_application_hits(payload)


def context_cache_config() -> ContextCacheConfig:
    return ContextCacheConfig(
        min_tokens=GEMINI_3_MIN_CACHE_TOKENS,
        ttl_seconds=CONTEXT_CACHE_TTL_SECONDS,
    )


def build_credit_officer(
    data_store_id: str,
    *,
    pack: PolicyPackCache | None = None,
    searcher: Searcher | None = None,
) -> Agent:
    store = data_store_id.strip()
    if not store:
        raise ValueError("DATA_STORE is required")
    policy_pack = pack or PolicyPackCache(uri="")
    policy_pack.current()
    instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    bound_search = searcher or (
        lambda query, store_id=store: search_client_applications(store_id, query)
    )

    def lookup_application(
        application_id: str = "", customer_name: str = ""
    ) -> str:
        """Look up one client application by application_id or customer_name.

        Pass exactly one argument. Searches kb-credit-policies with filter
        corpus = client-applications. Does not judge the filing.
        """
        query = lookup_query(application_id, customer_name)
        if query is None:
            return NO_SEARCH_SENTENCE
        return format_lookup_hits(bound_search(query))

    lookup_application.data_store_id = store  # type: ignore[attr-defined]

    agent = Agent(
        name="InteractiveAgent",
        model=chat_model(),
        description=INTERACTIVE_DESCRIPTION,
        static_instruction=compose_static_instruction(instructions, policy_pack.text),
        tools=[FunctionTool(lookup_application)],
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )

    def refresh_policy_pack(callback_context: object, llm_request: object) -> None:
        del callback_context
        _apply_pack_fill(agent, policy_pack, instructions, llm_request)

    agent.before_model_callback = refresh_policy_pack
    return agent


def build_credit_officer_app(
    data_store_id: str,
    *,
    pack: PolicyPackCache | None = None,
    searcher: Searcher | None = None,
) -> App:
    return App(
        name="credit_officer",
        root_agent=build_credit_officer(
            data_store_id, pack=pack, searcher=searcher
        ),
        context_cache_config=context_cache_config(),
    )


def load_root_agent(environ: Mapping[str, str] | None = None) -> Agent:
    env = os.environ if environ is None else environ
    store = resolve_data_store_id(env) or UNCONFIGURED_DATA_STORE
    pack = PolicyPackCache(uri=env.get("POLICY_PACK_URI", ""))
    pack.fill()
    return build_credit_officer(store, pack=pack)


def _apply_pack_fill(
    agent: Agent,
    pack: PolicyPackCache,
    instructions: str,
    llm_request: object,
) -> None:
    previous = pack.text
    previous_sha = pack.sha256
    pack.current()
    if pack.text == previous and pack.sha256 == previous_sha:
        return
    agent.static_instruction = compose_static_instruction(instructions, pack.text)
    config = getattr(llm_request, "config", None)
    if config is not None:
        config.system_instruction = agent.static_instruction


def _hit_text(derived: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("extractive_segments", "extractive_answers", "snippets"):
        values = derived.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                continue
            if not isinstance(value, dict):
                continue
            for field in ("content", "snippet"):
                text = value.get(field)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                    break
    return "\n".join(parts)


def _search_url(data_store_id: str) -> str:
    resource = data_store_id.strip().strip("/")
    return (
        "https://discoveryengine.googleapis.com/v1/"
        f"{resource}/servingConfigs/default_search:search"
    )


def _quota_project(resource: str) -> str:
    marker = "projects/"
    start = resource.find(marker)
    if start < 0:
        return ""
    rest = resource[start + len(marker) :]
    return rest.split("/", 1)[0]


def _post_discovery(url: str, body: dict[str, Any]) -> dict[str, Any]:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(Request())
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }
    quota = _quota_project(url)
    if quota:
        headers["x-goog-user-project"] = quota
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Agent Search failed: HTTP {exc.code} {detail}") from exc
    parsed = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Agent Search returned no object")
    return parsed


def _reasoning_engine_app():
    # Agent Runtime registers query methods on the entrypoint. An LlmAgent has
    # none of them; AdkApp is the object the runtime knows how to serve.
    # enable_tracing records the model request, the reply, and tool results
    # on Cloud Trace spans. AdkApp omits those attributes unless this is set.
    # Context cache holds the static prefix when it meets the model minimum.
    # A cache miss still sends the last PolicyPack fill.
    app = build_credit_officer_app(
        resolve_data_store_id() or UNCONFIGURED_DATA_STORE,
        pack=PolicyPackCache(uri=os.environ.get("POLICY_PACK_URI", "")),
    )
    try:
        from vertexai.agent_engines import AdkApp
    except ImportError:
        return app.root_agent
    return AdkApp(app=app, enable_tracing=True)


root_agent = _reasoning_engine_app()
