from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from talos.constants import (
    CORPUS_IDS,
    DEFAULT_GOLDEN_LAYERS,
    GOLDEN_QUERY_IDS,
    REFUSAL_SENTENCE,
    TEAMS_CHECKLIST_QUERY_IDS,
    WATERMARK,
)
from talos.env import repo_root
from tests.helpers import (
    BANNED_BANK_NAMES,
    fact_value_cases,
    first_visible_line,
    load_facts,
    load_golden_queries,
    load_manifest,
    policies_dir,
    sha256_utf8,
)

pytestmark = pytest.mark.unit

_FACTS = load_facts()
_GOLDEN = load_golden_queries()
_FACT_CASES = fact_value_cases(_FACTS)


def test_facts_unique_ids() -> None:
    ids = [doc["id"] for doc in _FACTS["documents"]]
    assert ids == list(CORPUS_IDS)
    assert len(ids) == len(set(ids)) == 12


def test_fact_keys_are_unique_across_documents() -> None:
    keys = [key for doc in _FACTS["documents"] for key in doc["facts"]]
    assert len(keys) == len(set(keys))


def test_generated_files_exist_for_each_fact_doc() -> None:
    out = policies_dir()
    for doc in _FACTS["documents"]:
        path = out / doc["filename"]
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip()
        assert re.fullmatch(re.escape(doc["id"]) + r"-.+\.md", doc["filename"])


def test_policies_have_no_synthetic_watermark() -> None:
    assert "watermark" not in _FACTS
    for path in policies_dir().glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert WATERMARK not in text, path.name
        assert "Not a real bank policy" not in text, path.name
        assert first_visible_line(text).startswith("> Policy ID:"), path.name


@pytest.mark.parametrize("doc_id,filename,key,value", _FACT_CASES)
def test_each_fact_value_in_document(
    doc_id: str, filename: str, key: str, value: str
) -> None:
    text = (policies_dir() / filename).read_text(encoding="utf-8")
    assert value in text, f"{doc_id}.{key} value {value!r} missing from {filename}"


def test_manifest_matches_files() -> None:
    manifest = load_manifest()
    assert "watermark" not in manifest
    assert manifest["container"] == "credit-policies"
    documents = manifest["documents"]
    assert {item["id"] for item in documents} == set(CORPUS_IDS)
    assert len(documents) == 12
    out = policies_dir()
    for item in documents:
        path = out / item["filename"]
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        digest = sha256_utf8(text)
        assert item["content_sha256"] == digest, item["id"]
        assert item["content_sha256"] == item["content_sha256"].lower()
        assert len(item["content_sha256"]) == hashlib.sha256().digest_size * 2
        assert item["blob_path"] == item["filename"]
        assert item["id"] in CORPUS_IDS


def test_golden_catalog_has_eighteen_ids() -> None:
    ids = [query["id"] for query in _GOLDEN]
    assert ids == list(GOLDEN_QUERY_IDS)
    assert len(ids) == 18
    assert len(set(ids)) == 18


def test_golden_default_layers_never_teams() -> None:
    for query in _GOLDEN:
        layers = tuple(query.get("layers", DEFAULT_GOLDEN_LAYERS))
        assert layers, query["id"]
        assert "teams" not in layers, query["id"]
        for layer in layers:
            assert layer in {"retrieval", "agent"}, query["id"]


def test_retrieval_never_asserts_refusal_sentence() -> None:
    needle = REFUSAL_SENTENCE.lower()
    for query in _GOLDEN:
        layers = query.get("layers", list(DEFAULT_GOLDEN_LAYERS))
        if "retrieval" not in layers:
            continue
        for value in query.get("expected_contains") or []:
            assert needle not in str(value).lower(), query["id"]


def test_comparison_recall_mode_all() -> None:
    for query in _GOLDEN:
        if "comparison" not in query.get("tags", []):
            continue
        if "retrieval" in query.get("layers", list(DEFAULT_GOLDEN_LAYERS)):
            assert query.get("recall_mode") == "all", query["id"]


def test_negative_adversarial_are_agent_layer() -> None:
    for query in _GOLDEN:
        tags = set(query.get("tags", []))
        if not tags.intersection({"negative", "adversarial"}):
            continue
        assert query.get("layers") == ["agent"], query["id"]


def test_golden_queries_source_ids_exist() -> None:
    known = set(CORPUS_IDS)
    for query in _GOLDEN:
        for doc_id in query.get("expected_source_doc_ids") or []:
            assert doc_id in known, f"{query['id']} unknown source {doc_id}"


def test_retrieval_expected_contains_in_source_docs() -> None:
    out = policies_dir()
    filename_by_id = {doc["id"]: doc["filename"] for doc in _FACTS["documents"]}
    for query in _GOLDEN:
        layers = query.get("layers", list(DEFAULT_GOLDEN_LAYERS))
        if "retrieval" not in layers:
            continue
        source_ids = query.get("expected_source_doc_ids") or []
        expected = query.get("expected_contains") or []
        if not source_ids or not expected:
            continue
        combined = "".join(
            (out / filename_by_id[doc_id]).read_text(encoding="utf-8")
            for doc_id in source_ids
        )
        for value in expected:
            assert value in combined, f"{query['id']} missing {value!r} in sources"


def test_teams_checklist_ids_are_in_catalog() -> None:
    ids = {query["id"] for query in _GOLDEN}
    assert set(TEAMS_CHECKLIST_QUERY_IDS) <= ids


def test_no_banned_real_bank_names() -> None:
    texts = [_FACTS]
    for path in policies_dir().glob("*.md"):
        texts.append(path.read_text(encoding="utf-8"))
    blob = str(texts)
    for name in BANNED_BANK_NAMES:
        assert name not in blob, name


def test_corpus_contract_defaults_to_unit_marker() -> None:
    data = tomllib.loads((repo_root() / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["pytest"]["ini_options"]["addopts"] == "-m unit"
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    names = {entry.split(":", 1)[0] for entry in markers}
    assert names == {"unit", "ingestion", "retrieval", "agent", "teams"}


def test_committed_markdown_count_is_twelve() -> None:
    files = sorted(policies_dir().glob("*.md"))
    assert len(files) == 12
    extras = {path.name for path in policies_dir().iterdir() if path.suffix != ".md"}
    assert extras == {"manifest.json"}


def test_session_fixtures_load_catalog(
    facts: dict[str, Any],
    manifest: dict[str, Any],
    golden_queries: list[dict[str, Any]],
    repo_root: Path,
) -> None:
    assert len(facts["documents"]) == 12
    assert len(manifest["documents"]) == 12
    assert len(golden_queries) == 18
    assert (repo_root / "tests/fixtures/golden_queries.yaml").is_file()
