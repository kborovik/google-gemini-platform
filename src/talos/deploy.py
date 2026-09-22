from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from talos.application import seed_application_fixtures
from talos.constants import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CORPUS_DESCRIPTION,
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_FIXTURES_RELATIVE,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CONTAINER,
    DEFAULT_CORPUS,
    DEFAULT_OUTPUT_RELATIVE,
    MIN_APPLICATION_INDEXED_ITEMS,
    MIN_INDEXED_ITEMS,
    POLL_INTERVAL_SECONDS,
    WAIT_TIMEOUT_SECONDS,
)
from talos.env import repo_root
from talos.errors import TalosError
from talos.gemini import (
    corpus_create_body,
    file_uri,
    import_body,
    operation_url,
    rag_corpora_url,
    rag_files_url,
    rag_import_url,
)
from talos.generate import BlobStore, open_blob_store, sync_markdown_directory
from talos.rest import RestClient, raise_for_status

Echo = Callable[[str], None]


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


class RagOps(Protocol):
    def find_corpus(self, display_name: str) -> str | None: ...

    def create_corpus(self, display_name: str) -> str: ...

    def import_uris(self, corpus: str, uris: list[str]) -> str: ...

    def operation_done(self, name: str) -> tuple[bool, str | None]: ...

    def list_file_uris(self, corpus: str) -> list[str]: ...


@dataclass(frozen=True)
class DeployConfig:
    project: str
    location: str
    bucket: str
    corpus_display_name: str = DEFAULT_CORPUS
    policy_dir: Path | None = None
    application_dir: Path | None = None
    application_fixtures_dir: Path | None = None
    policy_prefix: str = DEFAULT_CONTAINER
    application_prefix: str = DEFAULT_APPLICATION_CONTAINER
    wait: bool = False
    skip_import: bool = False
    dry_run: bool = False
    force: bool = False


def local_corpus_size(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob("*.md"))


def split_counts(uris: list[str]) -> tuple[int, int]:
    policies = 0
    applications = 0
    for uri in uris:
        name = uri.rsplit("/", 1)[-1]
        if "client-applications" in uri or name.startswith("credit-application-"):
            applications += 1
        elif "credit-policies" in uri or name.startswith("CP-"):
            policies += 1
    return policies, applications


def gcs_prefix_uri(bucket: str, prefix: str) -> str:
    return f"gs://{bucket}/{prefix.strip('/')}/"


class VertexRagOps:
    def __init__(self, rest: RestClient, project: str, location: str) -> None:
        self._rest = rest
        self._project = project
        self._location = location

    def find_corpus(self, display_name: str) -> str | None:
        page_token = ""
        while True:
            url = rag_corpora_url(self._project, self._location)
            if page_token:
                url = f"{url}?pageToken={page_token}"
            response = self._rest.request("GET", url, timeout=60.0)
            raise_for_status(response, "list RAG corpora")
            payload = response.json if isinstance(response.json, dict) else {}
            for item in payload.get("ragCorpora") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("displayName") == display_name and item.get("name"):
                    return str(item["name"])
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return None

    def create_corpus(self, display_name: str) -> str:
        response = self._rest.request(
            "POST",
            rag_corpora_url(self._project, self._location),
            json_body=corpus_create_body(display_name, CORPUS_DESCRIPTION),
            timeout=60.0,
        )
        raise_for_status(response, "create RAG corpus")
        payload = response.json if isinstance(response.json, dict) else {}
        direct = _operation_resource_name(payload)
        if direct:
            return direct
        op_name = payload.get("name")
        if not isinstance(op_name, str) or not op_name:
            raise TalosError("create RAG corpus returned no operation", exit_code=1)
        import time

        for _ in range(30):
            done, error = self.operation_done(op_name)
            if error:
                raise TalosError(f"create RAG corpus failed: {error}", exit_code=1)
            if done:
                follow = self._rest.request(
                    "GET", operation_url(self._location, op_name), timeout=60.0
                )
                body = follow.json if isinstance(follow.json, dict) else {}
                name = _operation_resource_name(body)
                if not name:
                    raise TalosError(
                        "create RAG corpus finished without a corpus name",
                        exit_code=1,
                    )
                return name
            time.sleep(2)
        raise TalosError("create RAG corpus timed out", exit_code=1)

    def import_uris(self, corpus: str, uris: list[str]) -> str:
        response = self._rest.request(
            "POST",
            rag_import_url(corpus, self._location),
            json_body=import_body(
                uris, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
            ),
            timeout=120.0,
        )
        raise_for_status(response, "import RAG files")
        payload = response.json if isinstance(response.json, dict) else {}
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise TalosError("import RAG files returned no operation name", exit_code=1)
        return name

    def operation_done(self, name: str) -> tuple[bool, str | None]:
        response = self._rest.request(
            "GET", operation_url(self._location, name), timeout=60.0
        )
        raise_for_status(response, "poll RAG operation")
        payload = response.json if isinstance(response.json, dict) else {}
        if not payload.get("done"):
            return False, None
        error = payload.get("error")
        if isinstance(error, dict) and error:
            message = error.get("message") or str(error)
            return True, str(message)
        return True, None

    def list_file_uris(self, corpus: str) -> list[str]:
        uris: list[str] = []
        page_token = ""
        while True:
            url = rag_files_url(corpus, self._location)
            if page_token:
                url = f"{url}?pageToken={page_token}"
            response = self._rest.request("GET", url, timeout=60.0)
            raise_for_status(response, "list RAG files")
            payload = response.json if isinstance(response.json, dict) else {}
            for item in payload.get("ragFiles") or []:
                if isinstance(item, dict):
                    uri = file_uri(item)
                    if uri:
                        uris.append(uri)
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return uris


def _operation_resource_name(payload: dict[str, Any]) -> str | None:
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("name"), str):
        name = response["name"]
        if "/ragCorpora/" in name:
            return name
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("ragCorpus", "resourceName"):
            value = metadata.get(key)
            if isinstance(value, str) and "/ragCorpora/" in value:
                return value
    name = payload.get("name")
    if isinstance(name, str) and "/ragCorpora/" in name and "/operations/" not in name:
        return name
    return None


def run_deploy(
    config: DeployConfig,
    *,
    rag: RagOps | None = None,
    blob_stores: dict[str, BlobStore] | None = None,
    clock: Clock | None = None,
    echo: Echo = print,
) -> None:
    root = repo_root()
    policy_dir = config.policy_dir or (root / DEFAULT_OUTPUT_RELATIVE)
    application_dir = config.application_dir or (
        root / DEFAULT_APPLICATION_OUTPUT_RELATIVE
    )
    fixture_dir = config.application_fixtures_dir
    if fixture_dir is None:
        fixture_dir = root / DEFAULT_APPLICATION_FIXTURES_RELATIVE

    if config.dry_run:
        echo(f"dry-run: would seed application fixtures from {fixture_dir}")
        echo(
            f"dry-run: would upload {gcs_prefix_uri(config.bucket, config.policy_prefix)}"
        )
        echo(
            "dry-run: would upload "
            + gcs_prefix_uri(config.bucket, config.application_prefix)
        )
        echo(f"dry-run: would ensure RAG corpus {config.corpus_display_name}")
        if not config.skip_import:
            echo(
                "dry-run: would import "
                + gcs_prefix_uri(config.bucket, config.policy_prefix)
                + " and "
                + gcs_prefix_uri(config.bucket, config.application_prefix)
            )
        return

    seeded = seed_application_fixtures(application_dir, fixture_dir)
    echo(f"seeded {seeded} application fixture files into {application_dir}")
    if config.wait:
        _assert_local_wait_ready(policy_dir, application_dir)

    stores = blob_stores or {}
    env = {
        "GOOGLE_CLOUD_PROJECT": config.project,
        "GCS_BUCKET": config.bucket,
    }
    for prefix, directory in (
        (config.policy_prefix, policy_dir),
        (config.application_prefix, application_dir),
    ):
        store = stores.get(prefix) or open_blob_store(
            env, container=prefix, bucket=config.bucket, purpose="deploy"
        )
        uploaded = sync_markdown_directory(
            store, directory, force=config.force, echo=echo
        )
        echo(f"blob-sync {prefix}: uploaded {uploaded}")

    ops = rag or _default_rag(config)
    corpus = ops.find_corpus(config.corpus_display_name)
    if corpus is None:
        echo(f"creating RAG corpus {config.corpus_display_name}")
        corpus = ops.create_corpus(config.corpus_display_name)
    else:
        echo(f"RAG corpus {config.corpus_display_name} exists ({corpus})")

    if config.skip_import:
        echo("skipping RAG import")
    else:
        uris = [
            gcs_prefix_uri(config.bucket, config.policy_prefix),
            gcs_prefix_uri(config.bucket, config.application_prefix),
        ]
        echo(f"importing {', '.join(uris)}")
        operation = ops.import_uris(corpus, uris)
        if config.wait:
            _wait_for_import(ops, operation, clock or SystemClock(), echo)
        else:
            echo(f"import operation {operation}")

    if config.wait:
        uris = ops.list_file_uris(corpus)
        policies, applications = split_counts(uris)
        _assert_indexed(
            policies,
            applications,
            local_corpus_size(application_dir),
        )
        echo(f"indexed policies={policies} applications={applications}")


def _default_rag(config: DeployConfig) -> VertexRagOps:
    from talos.rest import RequestsRest

    return VertexRagOps(RequestsRest(), config.project, config.location)


def _assert_local_wait_ready(policy_dir: Path, application_dir: Path) -> None:
    policies = local_corpus_size(policy_dir)
    applications = local_corpus_size(application_dir)
    if policies < MIN_INDEXED_ITEMS:
        raise TalosError(
            f"policy corpus has {policies} markdown files; need {MIN_INDEXED_ITEMS}. "
            "Run `uv run talos generate policy --local-only`.",
            exit_code=1,
        )
    if applications < MIN_APPLICATION_INDEXED_ITEMS:
        raise TalosError(
            f"application corpus has {applications} markdown files; "
            f"need at least {MIN_APPLICATION_INDEXED_ITEMS}. "
            "Run `uv run talos generate application --all --local-only`.",
            exit_code=1,
        )


def _assert_indexed(policies: int, applications: int, local_applications: int) -> None:
    if policies < MIN_INDEXED_ITEMS:
        raise TalosError(
            f"RAG indexed {policies} policy files; need {MIN_INDEXED_ITEMS}",
            exit_code=1,
        )
    floor = max(MIN_APPLICATION_INDEXED_ITEMS, local_applications)
    if applications < floor:
        raise TalosError(
            f"RAG indexed {applications} application files; need {floor}",
            exit_code=1,
        )


def _wait_for_import(ops: RagOps, operation: str, clock: Clock, echo: Echo) -> None:
    deadline = clock.monotonic() + WAIT_TIMEOUT_SECONDS
    while True:
        done, error = ops.operation_done(operation)
        if error:
            raise TalosError(f"RAG import failed: {error}", exit_code=1)
        if done:
            echo(f"import operation {operation} done")
            return
        if clock.monotonic() >= deadline:
            raise TalosError(
                f"RAG import timed out after {WAIT_TIMEOUT_SECONDS}s ({operation})",
                exit_code=1,
            )
        echo(f"waiting for import {operation}")
        clock.sleep(POLL_INTERVAL_SECONDS)
