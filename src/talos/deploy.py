from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from talos.application import seed_application_fixtures
from talos.constants import (
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_FIXTURES_RELATIVE,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CONTAINER,
    DEFAULT_CORPUS,
    DEFAULT_OUTPUT_RELATIVE,
)
from talos.env import repo_root
from talos.generate import BlobStore, open_blob_store, sync_markdown_directory

Echo = Callable[[str], None]


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


def run_deploy(
    config: DeployConfig,
    *,
    blob_stores: dict[str, BlobStore] | None = None,
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
        if config.skip_import:
            echo("dry-run: skipping data store import")
        else:
            echo(
                "dry-run: index is `gmake index` "
                f"for data store {config.corpus_display_name}"
            )
        return

    seeded = seed_application_fixtures(application_dir, fixture_dir)
    echo(f"seeded {seeded} application fixture files into {application_dir}")

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

    if config.skip_import:
        echo("skipping data store import")
        return
    if config.wait:
        echo(
            "uploaded; index with `gmake index wait=1` "
            f"for data store {config.corpus_display_name}"
        )
    else:
        echo(
            f"uploaded; index with `gmake index` for data store {config.corpus_display_name}"
        )
