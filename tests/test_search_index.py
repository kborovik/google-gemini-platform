from __future__ import annotations

import json
from pathlib import Path

import pytest

from docgen.constants import CLIENT_APPLICATIONS_CORPUS, MIN_APPLICATION_INDEXED_ITEMS
from docgen.env import repo_root
from docgen.errors import TalosError
from docgen.rest import RestResponse
from docgen.search_index import (
    DATA_STORE_LOCATION,
    IndexConfig,
    VertexSearchOps,
    corpus_metadata_documents,
    corpus_schema_body,
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
        self.done: list[tuple[bool, str | None]] = []
        self.uris: list[str] = []

    def import_uris(
        self,
        data_store_id: str,
        uris: list[str],
        *,
        corpus: str | None = None,
    ) -> str:
        self.calls.append(("import", data_store_id, tuple(uris), corpus))
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
    apps = tmp_path / "apps"
    apps.mkdir(exist_ok=True)
    application_count = int(overrides.pop("application_count", 3))  # type: ignore[arg-type]
    for index in range(application_count):
        (apps / f"credit-application-{index}.md").write_text(
            "# app\n", encoding="utf-8"
        )
    values: dict[str, object] = dict(
        project="lab5-gemini-dev1",
        bucket="lab5-gemini-dev1-credit-docs",
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
    assert "locations/global/" in import_url("lab5-gemini-dev1", "kb-credit-policies")


def test_index_does_not_create_the_data_store(tmp_path: Path) -> None:
    source = (repo_root() / "src/docgen/search_index.py").read_text(encoding="utf-8")
    assert "create_data_store" not in source
    assert "dataStores?" not in source
    assert "documents:import" in source
    search = FakeSearch()
    lines: list[str] = []
    run_index(_config(tmp_path, dry_run=True), ops=search, echo=lines.append)
    assert search.calls == []
    text = "\n".join(lines).lower()
    assert "would import" in text
    assert "kb-credit-policies" in text
    assert "creat" not in text
    assert "ensure" not in text


def test_import_sends_client_applications_only(tmp_path: Path) -> None:
    search = FakeSearch()
    run_index(_config(tmp_path), ops=search, echo=lambda _: None)
    imports = [call for call in search.calls if call[0] == "import"]
    assert len(imports) == 1
    application_uri = gcs_markdown_glob(
        "lab5-gemini-dev1-credit-docs", "client-applications"
    )
    assert imports[0][2] == (application_uri,)
    assert imports[0][3] == CLIENT_APPLICATIONS_CORPUS
    joined = " ".join(imports[0][2])
    assert "credit-policies" not in joined
    assert "policy-pack.md" not in joined
    content_body = import_documents_body([application_uri])
    encoded = json.dumps(content_body)
    assert content_body["gcsSource"]["dataSchema"] == "content"
    assert "corpus" not in encoded
    assert "rag" not in encoded.lower()
    assert "us-east5" not in encoded
    documents = corpus_metadata_documents(
        "lab5-gemini-dev1-credit-docs",
        "client-applications",
        [
            "credit-application-CA-2.md",
            "manifest.json",
            "policy-pack.md",
            "credit-application-CA-1.md",
        ],
        CLIENT_APPLICATIONS_CORPUS,
    )
    assert [item["content"]["uri"] for item in documents] == [
        "gs://lab5-gemini-dev1-credit-docs/client-applications/credit-application-CA-1.md",
        "gs://lab5-gemini-dev1-credit-docs/client-applications/credit-application-CA-2.md",
    ]
    assert {item["structData"]["corpus"] for item in documents} == {
        CLIENT_APPLICATIONS_CORPUS
    }
    schema = corpus_schema_body()
    assert schema["structSchema"]["properties"]["corpus"]["indexable"] is True


def test_wait_polls_indexed_counts_to_the_floors(tmp_path: Path) -> None:
    search = FakeSearch()
    search.uris = _indexed_uris(0, MIN_APPLICATION_INDEXED_ITEMS)
    run_index(
        _config(tmp_path, wait=True),
        ops=search,
        clock=FakeClock(),
        echo=lambda _: None,
    )
    assert any(call[0] == "list" for call in search.calls)


def test_wait_application_floor_is_max_of_three_and_local_size(tmp_path: Path) -> None:
    search = FakeSearch()
    search.uris = _indexed_uris(0, 3)
    with pytest.raises(TalosError, match="applications>=4"):
        run_index(
            _config(tmp_path, wait=True, application_count=4),
            ops=search,
            clock=FakeClock(),
            echo=lambda _: None,
        )
    search.uris = _indexed_uris(0, 4)
    run_index(
        _config(tmp_path, wait=True, application_count=4),
        ops=search,
        clock=FakeClock(),
        echo=lambda _: None,
    )


def test_wait_fails_before_cloud_when_local_applications_are_short(
    tmp_path: Path,
) -> None:
    search = FakeSearch()
    with pytest.raises(TalosError, match="application corpus"):
        run_index(
            _config(tmp_path, wait=True, application_count=2),
            ops=search,
            echo=lambda _: None,
        )
    assert search.calls == []


def test_import_error_fails(tmp_path: Path) -> None:
    search = FakeSearch()
    search.done = [(True, "discovery quota")]
    search.uris = _indexed_uris(0, 3)
    with pytest.raises(TalosError, match="discovery quota"):
        run_index(
            _config(tmp_path, wait=True),
            ops=search,
            clock=FakeClock(),
            echo=lambda _: None,
        )


class _Markdown:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.uploads: list[tuple[str, str, str]] = []

    def list_markdown(self, bucket: str, prefix: str) -> list[str]:
        del bucket, prefix
        return list(self.names)

    def upload_text(self, bucket: str, object_name: str, text: str) -> None:
        self.uploads.append((bucket, object_name, text))


class _Rest:
    def __init__(self, patch_status: int = 200) -> None:
        self.patch_status = patch_status
        self.calls: list[tuple[str, str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object = None,
        timeout: float = 60.0,
    ) -> RestResponse:
        del timeout
        self.calls.append((method, url, json_body))
        if method == "GET":
            return RestResponse(200, {"name": "operations/schema", "done": True}, "")
        if method == "PATCH" and self.patch_status == 404:
            return RestResponse(404, {}, "missing")
        return RestResponse(200, {"name": "operations/import-9"}, "")


def test_vertex_import_stamps_corpus_on_client_applications_only() -> None:
    markdown = _Markdown(
        [
            "credit-application-CA-9.md",
            "manifest.json",
            "policy-pack.md",
        ]
    )
    rest = _Rest()
    ops = VertexSearchOps(rest, "lab5-gemini-dev1", markdown=markdown)
    ops.import_uris("kb-credit-policies", ["gs://bucket/notes/*.md"])
    content_body = rest.calls[0][2]
    assert isinstance(content_body, dict)
    assert content_body["gcsSource"]["dataSchema"] == "content"
    assert "corpus" not in json.dumps(content_body)
    rest.calls.clear()
    ops.import_uris(
        "kb-credit-policies",
        ["gs://bucket/client-applications/*.md"],
        corpus=CLIENT_APPLICATIONS_CORPUS,
    )
    assert markdown.uploads[0][0] == "bucket"
    assert markdown.uploads[0][1] == "client-applications/_corpus.jsonl"
    document = json.loads(markdown.uploads[0][2])
    assert document["structData"]["corpus"] == CLIENT_APPLICATIONS_CORPUS
    assert document["content"]["uri"].endswith("/credit-application-CA-9.md")
    assert "manifest.json" not in markdown.uploads[0][2]
    assert "policy-pack.md" not in markdown.uploads[0][2]
    assert [call[0] for call in rest.calls] == ["PATCH", "GET", "POST"]
    schema = rest.calls[0][2]
    assert isinstance(schema, dict)
    assert schema["structSchema"]["properties"]["corpus"]["indexable"] is True
    imported = rest.calls[2][2]
    assert isinstance(imported, dict)
    assert imported["gcsSource"]["dataSchema"] == "document"
    assert imported["gcsSource"]["inputUris"] == [
        "gs://bucket/client-applications/_corpus.jsonl"
    ]


def test_corpus_schema_is_posted_when_the_default_schema_is_missing() -> None:
    rest = _Rest(patch_status=404)
    ops = VertexSearchOps(
        rest,
        "lab5-gemini-dev1",
        markdown=_Markdown(["credit-application-CA-1.md"]),
    )
    ops.import_uris(
        "kb-credit-policies",
        ["gs://bucket/client-applications/*.md"],
        corpus=CLIENT_APPLICATIONS_CORPUS,
    )
    assert [call[0] for call in rest.calls] == ["PATCH", "POST", "GET", "POST"]
    assert "schemaId=default_schema" in rest.calls[1][1]


def test_wait_does_not_finish_while_an_import_is_still_running(
    tmp_path: Path,
) -> None:
    search = FakeSearch()
    search.done = [(False, None)]
    search.uris = _indexed_uris(0, MIN_APPLICATION_INDEXED_ITEMS)
    clock = FakeClock()
    run_index(
        _config(tmp_path, wait=True),
        ops=search,
        clock=clock,
        echo=lambda _: None,
    )
    assert clock.sleeps


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
    assert (
        indexed_document_uri(
            {"content": {"uri": "gs://b/credit-policies/policy-pack.md"}}
        )
        == ""
    )
    assert (
        indexed_document_uri(
            {
                "content": {
                    "uri": "gs://b/client-applications/credit-application-CA-1.md"
                }
            }
        )
        == ""
    )
    assert (
        indexed_document_uri(
            {
                "content": {
                    "uri": "gs://b/client-applications/credit-application-CA-1.md"
                },
                "structData": {"corpus": "client-applications"},
            }
        )
        == "gs://b/client-applications/credit-application-CA-1.md"
    )
