from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from docgen.constants import (
    CLIENT_APPLICATIONS_CORPUS,
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CONTAINER,
    DEFAULT_CORPUS,
    DEFAULT_OUTPUT_RELATIVE,
    MIN_APPLICATION_INDEXED_ITEMS,
    MIN_INDEXED_ITEMS,
    POLICY_PACK_FILENAME,
    POLL_INTERVAL_SECONDS,
    WAIT_TIMEOUT_SECONDS,
)
from docgen.deploy import local_corpus_size, policy_markdown_paths, split_counts
from docgen.env import repo_root, require_env, resolve_env
from docgen.errors import TalosError
from docgen.rest import RestClient, raise_for_status

Echo = Callable[[str], None]
DATA_STORE_LOCATION = "global"
_COLLECTION = "default_collection"
_BRANCH = "0"


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


class SearchOps(Protocol):
    def import_uris(
        self,
        data_store_id: str,
        uris: list[str],
        *,
        corpus: str | None = None,
    ) -> str: ...

    def operation_done(self, name: str) -> tuple[bool, str | None]: ...

    def list_indexed_uris(self, data_store_id: str) -> list[str]: ...


class MarkdownStore(Protocol):
    def list_markdown(self, bucket: str, prefix: str) -> list[str]: ...

    def upload_text(self, bucket: str, object_name: str, text: str) -> None: ...


@dataclass(frozen=True)
class IndexConfig:
    project: str
    bucket: str
    data_store_id: str = DEFAULT_CORPUS
    policy_dir: Path | None = None
    application_dir: Path | None = None
    policy_prefix: str = DEFAULT_CONTAINER
    application_prefix: str = DEFAULT_APPLICATION_CONTAINER
    wait: bool = False
    dry_run: bool = False


def discoveryengine_root() -> str:
    return "https://discoveryengine.googleapis.com/v1"


def data_store_resource(project: str, data_store_id: str) -> str:
    return (
        f"projects/{project}/locations/{DATA_STORE_LOCATION}"
        f"/collections/{_COLLECTION}/dataStores/{data_store_id}"
    )


def data_store_url(project: str, data_store_id: str) -> str:
    return f"{discoveryengine_root()}/{data_store_resource(project, data_store_id)}"


def import_url(project: str, data_store_id: str) -> str:
    return (
        f"{data_store_url(project, data_store_id)}/branches/{_BRANCH}/documents:import"
    )


def documents_url(project: str, data_store_id: str) -> str:
    return f"{data_store_url(project, data_store_id)}/branches/{_BRANCH}/documents"


def operation_url(name: str) -> str:
    if name.startswith("https://"):
        return name
    return f"{discoveryengine_root()}/{name}"


def search_url(project: str, data_store_id: str) -> str:
    return (
        f"{data_store_url(project, data_store_id)}/servingConfigs/default_search:search"
    )


def gcs_markdown_glob(bucket: str, prefix: str) -> str:
    return f"gs://{bucket}/{prefix.strip('/')}/*.md"


def import_documents_body(
    uris: list[str], *, data_schema: str = "content"
) -> dict[str, Any]:
    return {
        "gcsSource": {
            "inputUris": uris,
            "dataSchema": data_schema,
        },
        "reconciliationMode": "INCREMENTAL",
    }


def corpus_schema_body() -> dict[str, Any]:
    """Make structData.corpus filterable. Does not create the data store."""
    return {
        "structSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "corpus": {
                    "type": "string",
                    "retrievable": True,
                    "indexable": True,
                    "searchable": False,
                }
            },
        }
    }


def corpus_document_id(filename: str) -> str:
    stem = filename[:-3] if filename.endswith(".md") else filename
    doc_id = stem.lower()
    body = doc_id.replace("-", "")
    if doc_id and doc_id[0].isalpha() and len(doc_id) <= 63 and body.isalnum():
        return doc_id
    return "a" + hashlib.sha256(filename.encode("utf-8")).hexdigest()[:32]


def corpus_metadata_documents(
    bucket: str,
    prefix: str,
    names: list[str],
    corpus: str,
) -> list[dict[str, Any]]:
    """One document per application Markdown file. Policies are not included."""
    documents: list[dict[str, Any]] = []
    folder = prefix.strip("/")
    for name in sorted(names):
        if name == POLICY_PACK_FILENAME or name == "manifest.json":
            continue
        if not name.endswith(".md") or "/" in name:
            continue
        documents.append(
            {
                "id": corpus_document_id(name),
                "structData": {"corpus": corpus},
                "content": {
                    "mimeType": "text/plain",
                    "uri": f"gs://{bucket}/{folder}/{name}",
                },
            }
        )
    return documents


def corpus_metadata_jsonl(
    bucket: str,
    prefix: str,
    names: list[str],
    corpus: str,
) -> str:
    documents = corpus_metadata_documents(bucket, prefix, names, corpus)
    if not documents:
        return ""
    return "".join(json.dumps(document) + "\n" for document in documents)


def parse_markdown_glob(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://") or not uri.endswith("/*.md"):
        raise TalosError(f"not a markdown glob: {uri}", exit_code=1)
    rest = uri.removeprefix("gs://")
    bucket, _, prefix = rest[: -len("/*.md")].partition("/")
    if not bucket or not prefix:
        raise TalosError(f"not a markdown glob: {uri}", exit_code=1)
    return bucket, prefix


def schema_url(project: str, data_store_id: str) -> str:
    return f"{data_store_url(project, data_store_id)}/schemas/default_schema"


def schemas_url(project: str, data_store_id: str) -> str:
    return f"{data_store_url(project, data_store_id)}/schemas?schemaId=default_schema"


def indexed_document_uri(document: dict[str, Any]) -> str:
    status = document.get("indexStatus")
    if isinstance(status, dict) and status.get("errorSamples"):
        return ""
    content = document.get("content")
    uri = ""
    if isinstance(content, dict) and isinstance(content.get("uri"), str):
        uri = content["uri"]
    if not uri:
        return ""
    name = uri.rsplit("/", 1)[-1]
    if name == POLICY_PACK_FILENAME:
        return ""
    if "client-applications" in uri or name.startswith("credit-application-"):
        struct = document.get("structData")
        corpus = struct.get("corpus") if isinstance(struct, dict) else None
        if corpus != CLIENT_APPLICATIONS_CORPUS:
            return ""
    return uri


def policy_object_uris(bucket: str, prefix: str, directory: Path) -> list[str]:
    """The 12 policy files. `policy-pack.md` is not an indexed policy document."""
    folder = prefix.strip("/")
    return [
        f"gs://{bucket}/{folder}/{path.name}"
        for path in policy_markdown_paths(directory)
    ]


class GcsMarkdown:
    def list_markdown(self, bucket: str, prefix: str) -> list[str]:
        from google.cloud import storage

        folder = prefix.strip("/") + "/"
        names: list[str] = []
        for blob in storage.Client().list_blobs(bucket, prefix=folder):
            name = str(blob.name).removeprefix(folder)
            if "/" in name or not name.endswith(".md"):
                continue
            if name == POLICY_PACK_FILENAME:
                continue
            names.append(name)
        return sorted(names)

    def upload_text(self, bucket: str, object_name: str, text: str) -> None:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(object_name)
        blob.upload_from_string(text.encode("utf-8"), content_type="application/jsonl")


class VertexSearchOps:
    def __init__(
        self,
        rest: RestClient,
        project: str,
        *,
        markdown: MarkdownStore | None = None,
    ) -> None:
        self._rest = rest
        self._project = project
        self._markdown = markdown or GcsMarkdown()

    def import_uris(
        self,
        data_store_id: str,
        uris: list[str],
        *,
        corpus: str | None = None,
    ) -> str:
        body = import_documents_body(uris)
        if corpus is not None:
            body = self._metadata_import_body(data_store_id, uris, corpus)
        response = self._rest.request(
            "POST",
            import_url(self._project, data_store_id),
            json_body=body,
            timeout=120.0,
        )
        raise_for_status(response, "import Agent Search documents")
        payload = response.json if isinstance(response.json, dict) else {}
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise TalosError(
                "import Agent Search documents returned no operation name",
                exit_code=1,
            )
        return name

    def _metadata_import_body(
        self, data_store_id: str, uris: list[str], corpus: str
    ) -> dict[str, Any]:
        if len(uris) != 1:
            raise TalosError(
                "corpus metadata import takes one prefix glob", exit_code=1
            )
        bucket, prefix = parse_markdown_glob(uris[0])
        names = self._markdown.list_markdown(bucket, prefix)
        jsonl = corpus_metadata_jsonl(bucket, prefix, names, corpus)
        if not jsonl:
            raise TalosError(
                f"no markdown under gs://{bucket}/{prefix} to stamp corpus={corpus}",
                exit_code=1,
            )
        object_name = f"{prefix.strip('/')}/_corpus.jsonl"
        self._markdown.upload_text(bucket, object_name, jsonl)
        self._ensure_corpus_schema(data_store_id)
        return import_documents_body(
            [f"gs://{bucket}/{object_name}"], data_schema="document"
        )

    def _ensure_corpus_schema(self, data_store_id: str) -> None:
        body = corpus_schema_body()
        response = self._rest.request(
            "PATCH",
            schema_url(self._project, data_store_id),
            json_body=body,
            timeout=60.0,
        )
        if response.status_code == 404:
            response = self._rest.request(
                "POST",
                schemas_url(self._project, data_store_id),
                json_body=body,
                timeout=60.0,
            )
        raise_for_status(response, "update Agent Search corpus schema")
        self._wait_schema_operation(response)

    def _wait_schema_operation(self, response: Any) -> None:
        payload = response.json if isinstance(response.json, dict) else {}
        if payload.get("done") is True:
            error = _operation_error(payload)
            if error:
                raise TalosError(
                    f"Agent Search schema update failed: {error}", exit_code=1
                )
            return
        if "structSchema" in payload and not payload.get("name"):
            return
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return
        import time

        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while True:
            done, error = self.operation_done(name)
            if error:
                raise TalosError(
                    f"Agent Search schema update failed: {error}", exit_code=1
                )
            if done:
                return
            if time.monotonic() >= deadline:
                raise TalosError(
                    "Agent Search schema update did not finish", exit_code=1
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    def operation_done(self, name: str) -> tuple[bool, str | None]:
        response = self._rest.request("GET", operation_url(name), timeout=60.0)
        raise_for_status(response, "poll Agent Search operation")
        payload = response.json if isinstance(response.json, dict) else {}
        if not payload.get("done"):
            return False, None
        return True, _operation_error(payload)

    def list_indexed_uris(self, data_store_id: str) -> list[str]:
        uris: list[str] = []
        page_token = ""
        while True:
            url = documents_url(self._project, data_store_id)
            params = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            query = urlencode(params)
            response = self._rest.request("GET", f"{url}?{query}", timeout=60.0)
            raise_for_status(response, "list Agent Search documents")
            payload = response.json if isinstance(response.json, dict) else {}
            for item in payload.get("documents") or []:
                if isinstance(item, dict):
                    uri = indexed_document_uri(item)
                    if uri:
                        uris.append(uri)
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return uris


def run_index(
    config: IndexConfig,
    *,
    ops: SearchOps | None = None,
    clock: Clock | None = None,
    echo: Echo = print,
) -> None:
    root = repo_root()
    policy_dir = config.policy_dir or (root / DEFAULT_OUTPUT_RELATIVE)
    application_dir = config.application_dir or (
        root / DEFAULT_APPLICATION_OUTPUT_RELATIVE
    )
    policy_uris = policy_object_uris(
        config.bucket, config.policy_prefix, policy_dir
    )
    application_uri = gcs_markdown_glob(config.bucket, config.application_prefix)
    store = data_store_resource(config.project, config.data_store_id)
    local_applications = local_corpus_size(application_dir)
    if config.wait:
        local_applications = _assert_local_wait_ready(policy_dir, application_dir)
    if config.dry_run:
        echo(
            f"dry-run: would import {len(policy_uris)} policy files, "
            f"{application_uri} into {store}; "
            f"stamp corpus={CLIENT_APPLICATIONS_CORPUS} on {config.application_prefix}"
        )
        return

    if not policy_uris:
        raise TalosError(
            f"policy corpus has no markdown files in {policy_dir}",
            exit_code=1,
        )
    search = ops or _default_ops(config)
    ticker = clock or SystemClock()
    echo(f"importing {len(policy_uris)} policy files into {store}")
    policy_operation = search.import_uris(config.data_store_id, policy_uris)
    _await_operation(search, policy_operation, ticker)
    echo(
        f"importing {application_uri} into {store} "
        f"with corpus={CLIENT_APPLICATIONS_CORPUS}"
    )
    application_operation = search.import_uris(
        config.data_store_id,
        [application_uri],
        corpus=CLIENT_APPLICATIONS_CORPUS,
    )
    if not config.wait:
        echo(f"import operation {policy_operation}")
        echo(f"import operation {application_operation}")
        echo(f"data store {store}")
        return
    _wait_until_indexed(
        search,
        config.data_store_id,
        [policy_operation, application_operation],
        local_applications,
        clock or SystemClock(),
        echo,
    )
    echo(f"data store {store}")


def index_config_from_env(
    *,
    project: str | None = None,
    location: str | None = None,
    bucket: str | None = None,
    wait: bool = False,
    dry_run: bool = False,
    use_terraform: bool = True,
) -> IndexConfig:
    env = resolve_env(use_terraform=use_terraform)
    resolved_project = project or env.get("GOOGLE_CLOUD_PROJECT") or ""
    resolved_bucket = bucket or env.get("GCS_BUCKET") or ""
    checked = dict(env)
    checked["GOOGLE_CLOUD_PROJECT"] = resolved_project
    checked["GOOGLE_CLOUD_LOCATION"] = (
        location or env.get("GOOGLE_CLOUD_LOCATION") or ""
    )
    checked["GCS_BUCKET"] = resolved_bucket
    if not dry_run or not resolved_project or not resolved_bucket:
        require_env(checked)
    return IndexConfig(
        project=resolved_project,
        bucket=resolved_bucket,
        wait=wait,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    wait = False
    dry_run = False
    for arg in args:
        if arg == "--wait":
            wait = True
        elif arg == "--dry-run":
            dry_run = True
        else:
            print(f"unknown argument {arg}", file=sys.stderr)
            raise SystemExit(2)
    try:
        run_index(index_config_from_env(wait=wait, dry_run=dry_run))
    except TalosError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc


def _default_ops(config: IndexConfig) -> VertexSearchOps:
    from docgen.rest import RequestsRest

    return VertexSearchOps(
        RequestsRest(quota_project=config.project),
        config.project,
    )


def _assert_local_wait_ready(policy_dir: Path, application_dir: Path) -> int:
    policies = local_corpus_size(policy_dir)
    applications = local_corpus_size(application_dir)
    if policies < MIN_INDEXED_ITEMS:
        raise TalosError(
            f"policy corpus has {policies} markdown files; need {MIN_INDEXED_ITEMS}. "
            "Run `uv run docgen generate policy --local-only`.",
            exit_code=1,
        )
    if applications < MIN_APPLICATION_INDEXED_ITEMS:
        raise TalosError(
            f"application corpus has {applications} markdown files; "
            f"need at least {MIN_APPLICATION_INDEXED_ITEMS}. "
            "Run `uv run docgen generate application --all --local-only`.",
            exit_code=1,
        )
    return applications


def _wait_until_indexed(
    ops: SearchOps,
    data_store_id: str,
    operations: list[str],
    local_applications: int,
    clock: Clock,
    echo: Echo,
) -> None:
    floor = max(MIN_APPLICATION_INDEXED_ITEMS, local_applications)
    deadline = clock.monotonic() + WAIT_TIMEOUT_SECONDS
    pending = list(operations)
    while True:
        still: list[str] = []
        for operation in pending:
            done, error = ops.operation_done(operation)
            if error:
                raise TalosError(f"Agent Search import failed: {error}", exit_code=1)
            if not done:
                still.append(operation)
        pending = still
        policies, applications = split_counts(ops.list_indexed_uris(data_store_id))
        if (
            not pending
            and policies >= MIN_INDEXED_ITEMS
            and applications >= floor
        ):
            echo(f"indexed policies={policies} applications={applications}")
            return
        if clock.monotonic() >= deadline:
            raise TalosError(
                f"indexed policies={policies} applications={applications}; "
                f"need policies>={MIN_INDEXED_ITEMS} applications>={floor}",
                exit_code=1,
            )
        if pending:
            echo(f"waiting for import {', '.join(pending)}")
        else:
            echo("import operations done; waiting for indexed counts")
        clock.sleep(POLL_INTERVAL_SECONDS)


def _await_operation(ops: SearchOps, name: str, clock: Clock) -> None:
    deadline = clock.monotonic() + WAIT_TIMEOUT_SECONDS
    while True:
        done, error = ops.operation_done(name)
        if error:
            raise TalosError(f"Agent Search import failed: {error}", exit_code=1)
        if done:
            return
        if clock.monotonic() >= deadline:
            raise TalosError(
                f"Agent Search import did not finish: {name}", exit_code=1
            )
        clock.sleep(POLL_INTERVAL_SECONDS)


def _operation_error(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict) and error:
        message = error.get("message") or str(error)
        return str(message)
    return None


if __name__ == "__main__":
    main()
