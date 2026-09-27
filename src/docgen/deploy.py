from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from docgen.application import seed_application_fixtures
from docgen.constants import (
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_FIXTURES_RELATIVE,
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CONTAINER,
    DEFAULT_OUTPUT_RELATIVE,
    POLICY_PACK_FILE_COUNT,
    POLICY_PACK_FILENAME,
)
from docgen.env import repo_root
from docgen.errors import TalosError
from docgen.generate import BlobStore, open_blob_store, sync_markdown_directory

Echo = Callable[[str], None]


@dataclass(frozen=True)
class DeployConfig:
    project: str
    location: str
    bucket: str
    policy_dir: Path | None = None
    application_dir: Path | None = None
    application_fixtures_dir: Path | None = None
    policy_prefix: str = DEFAULT_CONTAINER
    application_prefix: str = DEFAULT_APPLICATION_CONTAINER
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
        if name == POLICY_PACK_FILENAME:
            continue
        if "client-applications" in uri or name.startswith("credit-application-"):
            applications += 1
        elif "credit-policies" in uri or name.startswith("CP-"):
            policies += 1
    return policies, applications


def gcs_prefix_uri(bucket: str, prefix: str) -> str:
    return f"gs://{bucket}/{prefix.strip('/')}/"


def policy_markdown_paths(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.glob("*.md")
        if path.name != POLICY_PACK_FILENAME
    )


def render_policy_pack(directory: Path) -> tuple[bytes, str]:
    """12 policy Markdown files, lexicographic filename order.

    Each file is preceded by its filename. `manifest.json` is not Markdown,
    so it is excluded. Returns UTF-8 bytes and lowercase content_sha256.
    """
    paths = policy_markdown_paths(directory)
    if len(paths) != POLICY_PACK_FILE_COUNT:
        raise TalosError(
            f"policy pack needs {POLICY_PACK_FILE_COUNT} markdown files; "
            f"found {len(paths)} in {directory}",
            exit_code=1,
        )
    chunks = []
    for path in paths:
        text = path.read_text(encoding="utf-8").rstrip("\n")
        chunks.append(f"{path.name}\n{text}")
    payload = ("\n".join(chunks) + "\n").encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def publish_policy_pack(
    store: BlobStore,
    directory: Path,
    *,
    force: bool,
    echo: Echo,
) -> None:
    """Write credit-policies/policy-pack.md. Skip when content_sha256 matches."""
    data, digest = render_policy_pack(directory)
    existing = None if force else store.existing_sha256(POLICY_PACK_FILENAME)
    if existing is not None and existing.lower() == digest:
        echo(
            f"{POLICY_PACK_FILENAME}  blob={store.blob_url(POLICY_PACK_FILENAME)}  skipped"
        )
        return
    url = store.upload_markdown(
        POLICY_PACK_FILENAME, data, {"content_sha256": digest}
    )
    echo(f"{POLICY_PACK_FILENAME}  blob={url}  uploaded")


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
        echo(
            "dry-run: would write "
            + gcs_prefix_uri(config.bucket, config.policy_prefix)
            + POLICY_PACK_FILENAME
        )
        return

    seeded = seed_application_fixtures(application_dir, fixture_dir)
    echo(f"seeded {seeded} application fixture files into {application_dir}")

    stores = blob_stores or {}
    env = {
        "GOOGLE_CLOUD_PROJECT": config.project,
        "GCS_BUCKET": config.bucket,
    }
    policy_store: BlobStore | None = None
    for prefix, directory in (
        (config.policy_prefix, policy_dir),
        (config.application_prefix, application_dir),
    ):
        store = stores.get(prefix) or open_blob_store(
            env, container=prefix, bucket=config.bucket, purpose="deploy"
        )
        if prefix == config.policy_prefix:
            policy_store = store
        # policy-pack.md is published after the prefix sync. Keeping the name
        # stops that sync from deleting the object before the hash check.
        preserve = (
            {POLICY_PACK_FILENAME} if prefix == config.policy_prefix else None
        )
        uploaded = sync_markdown_directory(
            store, directory, force=config.force, echo=echo, preserve=preserve
        )
        echo(f"blob-sync {prefix}: uploaded {uploaded}")
    if policy_store is None:
        raise TalosError("policy prefix store is missing", exit_code=1)
    publish_policy_pack(
        policy_store, policy_dir, force=config.force, echo=echo
    )
