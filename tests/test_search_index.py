from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.constants import MIN_APPLICATION_INDEXED_ITEMS, MIN_INDEXED_ITEMS
from talos.errors import TalosError
from talos.search_index import (
    DATA_STORE_LOCATION,
    IndexConfig,
    data_store_body,
    data_store_resource,
    gcs_markdown_glob,
    import_documents_body,
    import_url,
    indexed_document_uri,
    run_index,
)

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.store: str | None = None
        self.done: list[tuple[bool, str | None]] = []
        self.uris: list[str] = []

    def find_data_store(self, data_store_id: str) -> str | None:
        self.calls.append(("find", data_store_id))
        return self.store

    def create_data_store(self, data_store_id: str) -> str:
        self.calls.append(("create", data_store_id))
        self.store = data_store_resource("lab5-gemini-dev1", data_store_id)
        return self.store

    def import_uris(self, data_store_id: str, uris: list[str]) -> str:
        self.calls.append(("import", data_store_id, tuple(uris)))
        return "operations/import-1"

    def operation_done(self, name: str) -> tuple[bool, str | None]:
        self.calls.append(("poll", name))
        if self.done:
            return self.done.pop(0)
        return True, None

    def list_indexed_uris(self, data_store_id: str) -> list[str]:
        self.calls.append(("list", data_store_id))
        return list(self.uris)


def _config(tmp_path: Path, **overrides: object) -> IndexConfig:
    policy = tmp_path / "policies"
    apps = tmp_path / "apps"
    policy.mkdir(exist_ok=True)
    apps.mkdir(exist_ok=True)
    policy_count = int(overrides.pop("policy_count", 12))  # type: ignore[arg-type]
    application_count = int(overrides.pop("application_count", 3))  # type: ignore[arg-type]
    for index in range(policy_count):
        (policy / f"CP-{index}.md").write_text(f"# {index}\n", encoding="utf-8")
    for index in range(application_count):
        (apps / f"credit-application-{index}.md").write_text(
            "# app\n", encoding="utf-8"
        )
    values: dict[str, object] = dict(
        project="lab5-gemini-dev1",
        bucket="lab5-gemini-dev1-credit-docs",
        policy_dir=policy,
        application_dir=apps,
    )
    values.update(overrides)
    return IndexConfig(**values)  # type: ignore[arg-type]


def _indexed_uris(policies: int, applications: int) -> list[str]:
    return [f"gs://b/credit-policies/{index}.md" for index in range(policies)] + [
        f"gs://b/client-applications/{index}.md" for index in range(applications)
    ]


def test_data_store_is_global_for_the_workload_project() -> None:
    resource = data_store_resource("lab5-gemini-dev1", "kb-credit-policies")
    assert DATA_STORE_LOCATION == "global"
    assert resource == (
        "projects/lab5-gemini-dev1/locations/global/collections/"
        "default_collection/dataStores/kb-credit-policies"
    )
    assert "us-east1" not in resource
    assert "us-east5" not in resource
    body = data_store_body("kb-credit-policies")
    encoded = json.dumps(body)
    assert "text-embedding" not in encoded
    assert "us-east5" not in encoded
    assert body["solutionTypes"] == ["SOLUTION_TYPE_SEARCH"]
    assert "locations/global/" in import_url("lab5-gemini-dev1", "kb-credit-policies")


def test_import_sends_both_markdown_prefixes(tmp_path: Path) -> None:
    search = FakeSearch()
    run_index(_config(tmp_path), ops=search, echo=lambda _: None)
    assert ("create", "kb-credit-policies") in search.calls
    imported = next(call for call in search.calls if call[0] == "import")
    assert imported[2] == (
        gcs_markdown_glob("lab5-gemini-dev1-credit-docs", "credit-policies"),
        gcs_markdown_glob("lab5-gemini-dev1-credit-docs", "client-applications"),
    )
    body = import_documents_body(list(imported[2]))
    encoded = json.dumps(body)
    assert body["gcsSource"]["dataSchema"] == "content"
    assert "rag" not in encoded.lower()
    assert "us-east5" not in encoded


def test_wait_polls_indexed_counts_to_the_floors(tmp_path: Path) -> None:
    search = FakeSearch()
    search.uris = _indexed_uris(MIN_INDEXED_ITEMS, MIN_APPLICATION_INDEXED_ITEMS)
    run_index(
        _config(tmp_path, wait=True),
        ops=search,
        clock=FakeClock(),
        echo=lambda _: None,
    )
    assert any(call[0] == "list" for call in search.calls)


def test_wait_application_floor_is_max_of_three_and_local_size(tmp_path: Path) -> None:
    search = FakeSearch()
    search.uris = _indexed_uris(12, 3)
    with pytest.raises(TalosError, match="applications>=4"):
        run_index(
            _config(tmp_path, wait=True, application_count=4),
            ops=search,
            clock=FakeClock(),
            echo=lambda _: None,
        )
    search.uris = _indexed_uris(12, 4)
    run_index(
        _config(tmp_path, wait=True, application_count=4),
        ops=search,
        clock=FakeClock(),
        echo=lambda _: None,
    )


def test_wait_fails_before_cloud_when_local_policies_are_short(tmp_path: Path) -> None:
    search = FakeSearch()
    with pytest.raises(TalosError, match="policy corpus"):
        run_index(
            _config(tmp_path, wait=True, policy_count=11),
            ops=search,
            echo=lambda _: None,
        )
    assert search.calls == []


def test_import_error_fails(tmp_path: Path) -> None:
    search = FakeSearch()
    search.done = [(True, "discovery quota")]
    search.uris = _indexed_uris(12, 3)
    with pytest.raises(TalosError, match="discovery quota"):
        run_index(
            _config(tmp_path, wait=True),
            ops=search,
            clock=FakeClock(),
            echo=lambda _: None,
        )


def test_indexed_document_skips_error_samples() -> None:
    assert (
        indexed_document_uri({"content": {"uri": "gs://b/credit-policies/CP-1.md"}})
        == "gs://b/credit-policies/CP-1.md"
    )
    assert (
        indexed_document_uri(
            {
                "content": {"uri": "gs://b/credit-policies/CP-1.md"},
                "indexStatus": {"errorSamples": [{"message": "parse"}]},
            }
        )
        == ""
    )
