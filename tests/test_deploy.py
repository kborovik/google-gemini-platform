from __future__ import annotations

from pathlib import Path

import pytest

from talos.deploy import DeployConfig, local_corpus_size, run_deploy, split_counts
from tests.fakes import FakeBlobStore

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
            "gs://b/client-applications/c.md",
        ]
    )
    assert (policies, applications) == (2, 1)
    policies, applications = split_counts(
        ["CP-RML-2026-01-residential-mortgage.md", "credit-application-CA-1.md"]
    )
    assert (policies, applications) == (1, 1)


def test_dry_run_points_at_gmake_index(tmp_path: Path) -> None:
    lines: list[str] = []
    run_deploy(_config(tmp_path, dry_run=True), echo=lines.append)
    text = "\n".join(lines)
    assert "gmake index" in text
    assert "kb-credit-policies" in text
    assert "RAG" not in text


def test_deploy_uploads_both_prefixes(tmp_path: Path) -> None:
    stores = {
        "credit-policies": FakeBlobStore(container="credit-policies"),
        "client-applications": FakeBlobStore(container="client-applications"),
    }
    config = _config(tmp_path, wait=True)
    lines: list[str] = []
    run_deploy(
        config,
        blob_stores=stores,  # type: ignore[arg-type]
        echo=lines.append,
    )
    assert len(stores["credit-policies"].uploads) == 12
    assert len(stores["client-applications"].uploads) == 3
    assert local_corpus_size(config.policy_dir) == 12  # type: ignore[arg-type]
    assert any("gmake index wait=1" in line for line in lines)
    assert all("RAG" not in line for line in lines)
