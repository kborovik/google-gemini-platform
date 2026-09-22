from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from talos.env import repo_root
from talos.generate import first_visible_line

GOLDEN_RELATIVE = "tests/fixtures/golden_queries.yaml"
FACTS_RELATIVE = "corpus/facts.yaml"
MANIFEST_RELATIVE = "data/credit-policies/manifest.json"
POLICIES_RELATIVE = "data/credit-policies"
APPLICATION_FIXTURES_RELATIVE = "tests/fixtures/client-applications"
APPLICATION_OUTPUT_RELATIVE = "data/client-applications"

BANNED_BANK_NAMES = (
    "JPMorgan",
    "J.P. Morgan",
    "Wells Fargo",
    "Bank of America",
    "HSBC",
    "Barclays",
    "Goldman Sachs",
    "Citibank",
    "Chase Bank",
)

__all__ = [
    "BANNED_BANK_NAMES",
    "FACTS_RELATIVE",
    "GOLDEN_RELATIVE",
    "MANIFEST_RELATIVE",
    "POLICIES_RELATIVE",
    "APPLICATION_FIXTURES_RELATIVE",
    "APPLICATION_OUTPUT_RELATIVE",
    "fact_value_cases",
    "first_visible_line",
    "load_application_fixtures",
    "load_facts",
    "load_golden_queries",
    "load_manifest",
    "policies_dir",
    "sha256_utf8",
]


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_facts(root: Path | None = None) -> dict[str, Any]:
    path = (root or repo_root()) / FACTS_RELATIVE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{FACTS_RELATIVE} root must be a mapping")
    return data


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    path = (root or repo_root()) / MANIFEST_RELATIVE
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{MANIFEST_RELATIVE} root must be a mapping")
    return data


def load_golden_queries(root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or repo_root()) / GOLDEN_RELATIVE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        queries = data.get("queries", [])
    else:
        queries = data
    if not isinstance(queries, list):
        raise TypeError(f"{GOLDEN_RELATIVE} must be a list or a mapping with queries")
    parsed: list[dict[str, Any]] = []
    for item in queries:
        if not isinstance(item, dict):
            raise TypeError("each golden query must be a mapping")
        parsed.append(item)
    return parsed


def policies_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / POLICIES_RELATIVE


def load_application_fixtures(root: Path | None = None) -> list[dict[str, Any]]:
    from talos.application import iter_manifest_documents, parse_application_markdown

    base = (root or repo_root()) / APPLICATION_FIXTURES_RELATIVE
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    records: list[dict[str, Any]] = []
    for item in iter_manifest_documents(manifest):
        path = base / str(item["filename"])
        record = parse_application_markdown(path.read_text(encoding="utf-8"))
        slot = str(
            item.get("expected_outcome")
            or item.get("intended_outcome")
            or item.get("slot")
            or ""
        )
        record["product_family"] = item.get("product_family")
        record["intended_outcome"] = item.get("intended_outcome") or slot
        record["expected_outcome"] = item.get("expected_outcome") or slot
        record["application_type"] = slot
        record["expected_judgement"] = (
            item.get("expected_outcome") or item.get("intended_outcome") or slot
        )
        record["filename"] = item.get("filename")
        records.append(record)
    return records


def fact_value_cases(facts: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    cases: list[tuple[str, str, str, str]] = []
    for doc in facts["documents"]:
        for key, value in doc["facts"].items():
            cases.append((doc["id"], doc["filename"], str(key), str(value)))
    return cases
