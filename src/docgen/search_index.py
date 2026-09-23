from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from docgen.constants import (
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CONTAINER,
    DEFAULT_CORPUS,
    DEFAULT_OUTPUT_RELATIVE,
    MIN_APPLICATION_INDEXED_ITEMS,
    MIN_INDEXED_ITEMS,
    POLL_INTERVAL_SECONDS,
    WAIT_TIMEOUT_SECONDS,
)
from docgen.deploy import local_corpus_size, split_counts
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
    def find_data_store(self, data_store_id: str) -> str | None: ...

    def create_data_store(self, data_store_id: str) -> str: ...

    def import_uris(self, data_store_id: str, uris: list[str]) -> str: ...

    def operation_done(self, name: str) -> tuple[bool, str | None]: ...

    def list_indexed_uris(self, data_store_id: str) -> list[str]: ...


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


def data_store_create_url(project: str, data_store_id: str) -> str:
    parent = (
        f"{discoveryengine_root()}/projects/{project}/locations/{DATA_STORE_LOCATION}"
        f"/collections/{_COLLECTION}/dataStores"
    )
    return f"{parent}?{urlencode({'dataStoreId': data_store_id})}"


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


def data_store_body(display_name: str) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "industryVertical": "GENERIC",
        "contentConfig": "CONTENT_REQUIRED",
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],
    }


def import_documents_body(uris: list[str]) -> dict[str, Any]:
    return {
        "gcsSource": {
            "inputUris": uris,
            "dataSchema": "content",
        },
        "reconciliationMode": "INCREMENTAL",
    }


def indexed_document_uri(document: dict[str, Any]) -> str:
    status = document.get("indexStatus")
    if isinstance(status, dict) and status.get("errorSamples"):
        return ""
    content = document.get("content")
    if isinstance(content, dict) and isinstance(content.get("uri"), str):
        return content["uri"]
    return ""


class VertexSearchOps:
    def __init__(self, rest: RestClient, project: str) -> None:
        self._rest = rest
        self._project = project

    def find_data_store(self, data_store_id: str) -> str | None:
        response = self._rest.request(
            "GET",
            data_store_url(self._project, data_store_id),
            timeout=60.0,
        )
        if response.status_code == 404:
            return None
        raise_for_status(response, "get Agent Search data store")
        payload = response.json if isinstance(response.json, dict) else {}
        name = payload.get("name")
        if isinstance(name, str) and name:
            return name
        return data_store_resource(self._project, data_store_id)

    def create_data_store(self, data_store_id: str) -> str:
        response = self._rest.request(
            "POST",
            data_store_create_url(self._project, data_store_id),
            json_body=data_store_body(data_store_id),
            timeout=60.0,
        )
        raise_for_status(response, "create Agent Search data store")
        payload = response.json if isinstance(response.json, dict) else {}
        name = payload.get("name")
        if isinstance(name, str) and "/operations/" in name:
            error = _operation_error(payload)
            if error:
                raise TalosError(
                    f"create Agent Search data store failed: {error}", exit_code=1
                )
            if not payload.get("done"):
                self._wait_operation(name)
        return data_store_resource(self._project, data_store_id)

    def import_uris(self, data_store_id: str, uris: list[str]) -> str:
        response = self._rest.request(
            "POST",
            import_url(self._project, data_store_id),
            json_body=import_documents_body(uris),
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

    def _wait_operation(self, name: str) -> None:
        import time

        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while True:
            done, error = self.operation_done(name)
            if error:
                raise TalosError(
                    f"create Agent Search data store failed: {error}", exit_code=1
                )
            if done:
                return
            if time.monotonic() >= deadline:
                raise TalosError(
                    "create Agent Search data store timed out after "
                    f"{WAIT_TIMEOUT_SECONDS}s",
                    exit_code=1,
                )
            time.sleep(POLL_INTERVAL_SECONDS)


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
    uris = [
        gcs_markdown_glob(config.bucket, config.policy_prefix),
        gcs_markdown_glob(config.bucket, config.application_prefix),
    ]
    store = data_store_resource(config.project, config.data_store_id)
    local_applications = local_corpus_size(application_dir)
    if config.wait:
        local_applications = _assert_local_wait_ready(policy_dir, application_dir)
    if config.dry_run:
        echo(f"dry-run: would ensure Agent Search data store {store}")
        echo(f"dry-run: would import {', '.join(uris)}")
        return

    search = ops or _default_ops(config)
    found = search.find_data_store(config.data_store_id)
    if found is None:
        echo(f"creating Agent Search data store {config.data_store_id}")
        found = search.create_data_store(config.data_store_id)
    else:
        echo(f"Agent Search data store {config.data_store_id} exists ({found})")
    echo(f"importing {', '.join(uris)}")
    operation = search.import_uris(config.data_store_id, uris)
    if not config.wait:
        echo(f"import operation {operation}")
        echo(f"data store {store}")
        return
    _wait_until_indexed(
        search,
        config.data_store_id,
        operation,
        local_applications,
        clock or SystemClock(),
        echo,
    )
    echo(f"data store {store}")


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
        env = resolve_env(use_terraform=True)
        if not dry_run:
            require_env(env)
        elif not env.get("GOOGLE_CLOUD_PROJECT") or not env.get("GCS_BUCKET"):
            require_env(env)
        run_index(
            IndexConfig(
                project=env.get("GOOGLE_CLOUD_PROJECT") or "",
                bucket=env.get("GCS_BUCKET") or "",
                wait=wait,
                dry_run=dry_run,
            )
        )
    except TalosError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc


def _default_ops(config: IndexConfig) -> VertexSearchOps:
    from docgen.rest import RequestsRest

    return VertexSearchOps(RequestsRest(), config.project)


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
    operation: str,
    local_applications: int,
    clock: Clock,
    echo: Echo,
) -> None:
    floor = max(MIN_APPLICATION_INDEXED_ITEMS, local_applications)
    deadline = clock.monotonic() + WAIT_TIMEOUT_SECONDS
    while True:
        done, error = ops.operation_done(operation)
        if error:
            raise TalosError(f"Agent Search import failed: {error}", exit_code=1)
        policies, applications = split_counts(ops.list_indexed_uris(data_store_id))
        if policies >= MIN_INDEXED_ITEMS and applications >= floor:
            echo(f"indexed policies={policies} applications={applications}")
            return
        if clock.monotonic() >= deadline:
            raise TalosError(
                f"indexed policies={policies} applications={applications}; "
                f"need policies>={MIN_INDEXED_ITEMS} applications>={floor}",
                exit_code=1,
            )
        if done:
            echo(f"import operation {operation} done; waiting for indexed counts")
        else:
            echo(f"waiting for import {operation}")
        clock.sleep(POLL_INTERVAL_SECONDS)


def _operation_error(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict) and error:
        message = error.get("message") or str(error)
        return str(message)
    return None


if __name__ == "__main__":
    main()
