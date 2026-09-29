from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from docgen.deploy import (
    DeployConfig,
    local_corpus_size,
    render_policy_pack,
    run_deploy,
    split_counts,
)
from docgen.env import repo_root
from docgen.errors import TalosError
from tests.fakes import FakeBlob, FakeBlobStore

pytestmark = pytest.mark.unit


def _config(tmp_path: Path, **overrides: object) -> DeployConfig:
    policy = tmp_path / "policies"
    apps = tmp_path / "apps"
    policy.mkdir()
    apps.mkdir()
    for index in range(12):
        (policy / f"CP-{index}.md").write_text(f"# {index}\n", encoding="utf-8")
    for index in range(3):
        (apps / f"credit-application-{index}.md").write_text(
            "# app\n", encoding="utf-8"
        )
    values: dict[str, object] = dict(
        project="lab5-gemini-dev1",
        location="us-east1",
        bucket="lab5-gemini-dev1-credit-docs",
        policy_dir=policy,
        application_dir=apps,
        application_fixtures_dir=tmp_path / "missing-fixtures",
    )
    values.update(overrides)
    return DeployConfig(**values)  # type: ignore[arg-type]


def test_split_counts_by_prefix() -> None:
    policies, applications = split_counts(
        [
            "gs://b/credit-policies/a.md",
            "gs://b/credit-policies/b.md",
            "gs://b/credit-policies/policy-pack.md",
            "gs://b/client-applications/c.md",
        ]
    )
    assert (policies, applications) == (2, 1)
    policies, applications = split_counts(
        ["CP-RML-2026-01-residential-mortgage.md", "credit-application-CA-1.md"]
    )
    assert (policies, applications) == (1, 1)


def test_dry_run_lists_both_prefixes_and_does_not_import(tmp_path: Path) -> None:
    lines: list[str] = []
    run_deploy(_config(tmp_path, dry_run=True), echo=lines.append)
    text = "\n".join(lines)
    assert "gs://lab5-gemini-dev1-credit-docs/credit-policies/" in text
    assert "gs://lab5-gemini-dev1-credit-docs/client-applications/" in text
    assert "gs://lab5-gemini-dev1-credit-docs/credit-policies/policy-pack.md" in text
    assert "import" not in text.lower()
    assert "gmake index" not in text
    assert "RAG" not in text


def test_upload_writes_both_prefixes(tmp_path: Path) -> None:
    stores = {
        "credit-policies": FakeBlobStore(container="credit-policies"),
        "client-applications": FakeBlobStore(container="client-applications"),
    }
    config = _config(tmp_path)
    lines: list[str] = []
    run_deploy(
        config,
        blob_stores=stores,  # type: ignore[arg-type]
        echo=lines.append,
    )
    assert stores["credit-policies"].uploads[-1] == "policy-pack.md"
    assert len(stores["credit-policies"].uploads) == 13
    pack = stores["credit-policies"].blobs["policy-pack.md"]
    assert pack.metadata["content_sha256"] == hashlib.sha256(pack.data).hexdigest()
    assert len(stores["client-applications"].uploads) == 3
    assert local_corpus_size(config.policy_dir) == 12  # type: ignore[arg-type]
    assert all("import" not in line.lower() for line in lines)
    assert all("RAG" not in line for line in lines)


def test_upload_skips_matching_content_sha256(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stores: dict[str, FakeBlobStore] = {}
    for prefix, directory in (
        ("credit-policies", config.policy_dir),
        ("client-applications", config.application_dir),
    ):
        store = FakeBlobStore(container=prefix)
        assert directory is not None
        for path in directory.glob("*.md"):
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            store.blobs[path.name] = FakeBlob(
                data=data, metadata={"content_sha256": digest.upper()}
            )
        stores[prefix] = store
    pack_bytes, pack_digest = render_policy_pack(config.policy_dir)  # type: ignore[arg-type]
    stores["credit-policies"].blobs["policy-pack.md"] = FakeBlob(
        data=pack_bytes, metadata={"content_sha256": pack_digest.upper()}
    )
    lines: list[str] = []
    run_deploy(config, blob_stores=stores, echo=lines.append)  # type: ignore[arg-type]
    assert stores["credit-policies"].uploads == []
    assert stores["client-applications"].uploads == []
    assert any("policy-pack.md" in line and "skipped" in line for line in lines)
    assert all("import" not in line.lower() for line in lines)


def test_policy_pack_orders_twelve_files_and_skips_manifest(tmp_path: Path) -> None:
    policy = tmp_path / "policies"
    policy.mkdir()
    names = [f"CP-{index}.md" for index in range(12)]
    for name in reversed(names):
        (policy / name).write_text(f"body-{name}\n", encoding="utf-8")
    (policy / "manifest.json").write_text("SENTINEL-MANIFEST", encoding="utf-8")
    (policy / "policy-pack.md").write_text("stale pack\n", encoding="utf-8")
    data, digest = render_policy_pack(policy)
    text = data.decode("utf-8")
    assert "SENTINEL-MANIFEST" not in text
    assert "stale pack" not in text
    assert digest == hashlib.sha256(data).hexdigest()
    rest = text
    for name in sorted(names):
        prefix = f"{name}\nbody-{name}"
        assert rest.startswith(prefix)
        rest = rest[len(prefix) :]
        if rest.startswith("\n"):
            rest = rest[1:]
    assert rest == ""


def test_published_policies_match_the_twelve_file_pack() -> None:
    directory = repo_root() / "data" / "credit-policies"
    data, digest = render_policy_pack(directory)
    names = sorted(
        path.name for path in directory.glob("*.md") if path.name != "policy-pack.md"
    )
    assert len(names) == 12
    manifest = (directory / "manifest.json").read_text(encoding="utf-8")
    assert manifest not in data.decode("utf-8")
    rest = data.decode("utf-8")
    for name in names:
        body = (directory / name).read_text(encoding="utf-8").rstrip("\n")
        prefix = f"{name}\n{body}"
        assert rest.startswith(prefix), name
        rest = rest[len(prefix) :]
        if rest.startswith("\n"):
            rest = rest[1:]
    assert rest == ""
    assert digest == hashlib.sha256(data).hexdigest()


def test_policy_pack_rejects_a_short_directory(tmp_path: Path) -> None:
    directory = tmp_path / "short"
    directory.mkdir()
    (directory / "CP-1.md").write_text("only\n", encoding="utf-8")
    with pytest.raises(TalosError, match="12"):
        render_policy_pack(directory)


def test_changed_policy_pack_hash_is_uploaded(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stores: dict[str, FakeBlobStore] = {}
    for prefix, directory in (
        ("credit-policies", config.policy_dir),
        ("client-applications", config.application_dir),
    ):
        store = FakeBlobStore(container=prefix)
        assert directory is not None
        for path in directory.glob("*.md"):
            data = path.read_bytes()
            store.blobs[path.name] = FakeBlob(
                data=data,
                metadata={"content_sha256": hashlib.sha256(data).hexdigest()},
            )
        stores[prefix] = store
    stores["credit-policies"].blobs["policy-pack.md"] = FakeBlob(
        data=b"old", metadata={"content_sha256": "0" * 64}
    )
    run_deploy(config, blob_stores=stores, echo=lambda _: None)  # type: ignore[arg-type]
    assert stores["credit-policies"].uploads == ["policy-pack.md"]
    assert stores["client-applications"].uploads == []
    uploaded = stores["credit-policies"].blobs["policy-pack.md"]
    assert (
        uploaded.metadata["content_sha256"] == hashlib.sha256(uploaded.data).hexdigest()
    )
