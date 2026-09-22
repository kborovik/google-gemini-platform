from __future__ import annotations

from pathlib import Path

import pytest

from talos.deploy import DeployConfig, local_corpus_size, run_deploy, split_counts
from talos.errors import TalosError
from tests.fakes import FakeBlobStore

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


class FakeRag:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.corpus: str | None = None
        self.done: list[tuple[bool, str | None]] = []
        self.uris: list[str] = []

    def find_corpus(self, display_name: str) -> str | None:
        self.calls.append(("find", display_name))
        return self.corpus

    def create_corpus(self, display_name: str) -> str:
        self.calls.append(("create", display_name))
        self.corpus = f"projects/p/locations/us-east1/ragCorpora/{display_name}"
        return self.corpus

    def import_uris(self, corpus: str, uris: list[str]) -> str:
        self.calls.append(("import", corpus, tuple(uris)))
        return "operations/import-1"

    def operation_done(self, name: str) -> tuple[bool, str | None]:
        self.calls.append(("poll", name))
        if self.done:
            return self.done.pop(0)
        return True, None

    def list_file_uris(self, corpus: str) -> list[str]:
        self.calls.append(("list", corpus))
        return list(self.uris)


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


def test_dry_run_makes_no_rag_calls(tmp_path: Path) -> None:
    rag = FakeRag()
    run_deploy(_config(tmp_path, dry_run=True), rag=rag, echo=lambda _: None)
    assert rag.calls == []


def test_deploy_uploads_and_imports(tmp_path: Path) -> None:
    rag = FakeRag()
    stores = {
        "credit-policies": FakeBlobStore(container="credit-policies"),
        "client-applications": FakeBlobStore(container="client-applications"),
    }
    config = _config(tmp_path, wait=True)
    rag.uris = [f"gs://b/credit-policies/{index}.md" for index in range(12)] + [
        f"gs://b/client-applications/{index}.md" for index in range(3)
    ]
    run_deploy(
        config,
        rag=rag,
        blob_stores=stores,  # type: ignore[arg-type]
        clock=FakeClock(),
        echo=lambda _: None,
    )
    assert ("create", "kb-credit-policies") in rag.calls
    assert any(call[0] == "import" for call in rag.calls)
    assert len(stores["credit-policies"].uploads) == 12
    assert len(stores["client-applications"].uploads) == 3
    assert local_corpus_size(config.policy_dir) == 12  # type: ignore[arg-type]


def test_wait_fails_when_application_count_is_low(tmp_path: Path) -> None:
    rag = FakeRag()
    rag.uris = [f"gs://b/credit-policies/{index}.md" for index in range(12)]
    stores = {
        "credit-policies": FakeBlobStore(container="credit-policies"),
        "client-applications": FakeBlobStore(container="client-applications"),
    }
    with pytest.raises(TalosError, match="application"):
        run_deploy(
            _config(tmp_path, wait=True),
            rag=rag,
            blob_stores=stores,  # type: ignore[arg-type]
            clock=FakeClock(),
            echo=lambda _: None,
        )


def test_import_error_fails(tmp_path: Path) -> None:
    rag = FakeRag()
    rag.done = [(True, "embedding quota")]
    stores = {
        "credit-policies": FakeBlobStore(container="credit-policies"),
        "client-applications": FakeBlobStore(container="client-applications"),
    }
    with pytest.raises(TalosError, match="embedding quota"):
        run_deploy(
            _config(tmp_path, wait=True),
            rag=rag,
            blob_stores=stores,  # type: ignore[arg-type]
            clock=FakeClock(),
            echo=lambda _: None,
        )
