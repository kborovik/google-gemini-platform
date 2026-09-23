from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from docgen.cli import cli
from docgen.constants import CORPUS_IDS, WATERMARK
from docgen.env import repo_root
from docgen.errors import TalosError
from docgen.generate import (
    GenerateConfig,
    first_visible_line,
    load_and_validate_facts,
    resolve_bucket,
    run_generate,
    sync_markdown_directory,
)
from tests.fakes import FakeBlob, FakeBlobStore

pytestmark = pytest.mark.unit

FACTS = repo_root() / "corpus/facts.yaml"
TEMPLATES = repo_root() / "corpus/templates"


def _generate_args(out: Path, *extra: str) -> list[str]:
    return [
        "generate",
        "policy",
        "--local-only",
        "--out",
        str(out),
        "--facts",
        str(FACTS),
        "--templates",
        str(TEMPLATES),
        "--no-terraform",
        *extra,
    ]


def test_generate_help_documents_flags() -> None:
    result = CliRunner().invoke(cli, ["generate", "policy", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--out",
        "--facts",
        "--templates",
        "--local-only",
        "--gcs-only",
        "--dry-run",
        "--force",
        "--fail-if-missing-gcs",
        "--no-terraform",
    ):
        assert flag in result.output


def test_generate_local_only_writes_twelve_markdown_and_manifest(
    tmp_path: Path,
) -> None:
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(cli, _generate_args(out))
    assert result.exit_code == 0, result.output
    documents = yaml.safe_load(FACTS.read_text(encoding="utf-8"))["documents"]
    assert len(documents) == 12
    for doc in documents:
        path = out / doc["filename"]
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip()
    manifest = yaml.safe_load((out / "manifest.json").read_text(encoding="utf-8"))
    assert "watermark" not in manifest
    assert manifest["container"] == "credit-policies"
    assert {item["id"] for item in manifest["documents"]} == set(CORPUS_IDS)
    assert len(manifest["documents"]) == 12


def test_generated_policies_have_no_synthetic_watermark(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(cli, _generate_args(out))
    assert result.exit_code == 0, result.output
    for path in out.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert WATERMARK not in text
        assert "Not a real bank policy" not in text
        assert first_visible_line(text).startswith("> Policy ID:")


def test_fact_keys_are_unique_and_values_appear_verbatim(tmp_path: Path) -> None:
    documents = load_and_validate_facts(FACTS)
    keys = [key for doc in documents for key in doc.facts]
    assert len(keys) == len(set(keys))
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(cli, _generate_args(out))
    assert result.exit_code == 0, result.output
    for doc in documents:
        text = (out / doc.filename).read_text(encoding="utf-8")
        for value in doc.facts.values():
            assert value in text, f"{doc.id} missing {value!r}"


def test_threshold_sentences_use_fact_literals(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(cli, _generate_args(out))
    assert result.exit_code == 0, result.output
    rml = (out / "CP-RML-2026-01-residential-mortgage.md").read_text(encoding="utf-8")
    ucl = (out / "CP-UCL-2026-01-unsecured-consumer.md").read_text(encoding="utf-8")
    exc = (out / "CP-EXC-2026-01-exceptions-overrides.md").read_text(encoding="utf-8")
    assert "80%" in rml
    assert "eighty percent" not in rml.lower()
    assert "USD 50,000" in ucl
    assert "$50,000" not in ucl
    assert "Credit Exceptions Committee" in rml
    assert "Credit Exceptions Committee" in exc


def test_duplicate_fact_key_exits_3(tmp_path: Path) -> None:
    data = yaml.safe_load(FACTS.read_text(encoding="utf-8"))
    data["documents"][1]["facts"]["max_ltv_owner_occupied"] = "99%"
    facts_path = tmp_path / "facts.yaml"
    facts_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "policy",
            "--local-only",
            "--out",
            str(out),
            "--facts",
            str(facts_path),
            "--templates",
            str(TEMPLATES),
            "--no-terraform",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "duplicate fact key" in result.output
    assert not out.exists()


def test_invalid_yaml_exits_3(tmp_path: Path) -> None:
    facts_path = tmp_path / "facts.yaml"
    facts_path.write_text("{ not: valid: yaml", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "policy",
            "--local-only",
            "--out",
            str(tmp_path / "out"),
            "--facts",
            str(facts_path),
            "--templates",
            str(TEMPLATES),
            "--no-terraform",
        ],
    )
    assert result.exit_code == 3, result.output


def test_local_and_azure_only_are_mutex() -> None:
    result = CliRunner().invoke(
        cli, ["generate", "policy", "--local-only", "--gcs-only", "--no-terraform"]
    )
    assert result.exit_code == 1, result.output
    assert "mutually exclusive" in result.output


def test_fail_if_missing_azure_exits_2(clean_azure_env: None) -> None:
    result = CliRunner().invoke(
        cli, ["generate", "policy", "--fail-if-missing-gcs", "--no-terraform"]
    )
    assert result.exit_code == 2, result.output
    assert "Google Cloud environment is not configured" in result.output


def test_azure_only_missing_env_exits_2(clean_azure_env: None) -> None:
    result = CliRunner().invoke(
        cli, ["generate", "policy", "--gcs-only", "--no-terraform"]
    )
    assert result.exit_code == 2, result.output


def test_local_only_does_not_require_azure(
    tmp_path: Path, clean_azure_env: None
) -> None:
    result = CliRunner().invoke(
        cli, _generate_args(tmp_path / "out", "--fail-if-missing-gcs")
    )
    assert result.exit_code == 0, result.output


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(cli, _generate_args(out, "--dry-run"))
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert not out.exists()


def test_load_and_validate_facts_rejects_watermark(tmp_path: Path) -> None:
    data = yaml.safe_load(FACTS.read_text(encoding="utf-8"))
    data["watermark"] = WATERMARK
    path = tmp_path / "facts.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(TalosError) as exc:
        load_and_validate_facts(path)
    assert exc.value.exit_code == 3
    assert "watermark" in str(exc.value)


def _azure_config(out: Path, **overrides: object) -> GenerateConfig:
    values: dict[str, object] = dict(
        out=out,
        facts_path=FACTS,
        templates_dir=TEMPLATES,
        use_terraform=False,
        bucket="lab5-gemini-dev1-credit-docs",
    )
    values.update(overrides)
    return GenerateConfig(**values)  # type: ignore[arg-type]


def test_bucket_flag_overrides_env() -> None:
    assert resolve_bucket({"GCS_BUCKET": "from-env"}, bucket="from-flag") == "from-flag"


def test_bucket_falls_back_to_env() -> None:
    assert resolve_bucket({"GCS_BUCKET": "from-env"}) == "from-env"


def test_bucket_deploy_purpose_omits_local_only_hint() -> None:
    with pytest.raises(TalosError, match="infra/outputs.json") as exc:
        resolve_bucket({}, purpose="deploy")
    assert exc.value.exit_code == 2
    assert "--local-only" not in str(exc.value)


def test_gcs_store_uses_prefix_and_sha_metadata() -> None:
    from docgen.generate import GcsObjectStore

    class Blob:
        def __init__(self) -> None:
            self.metadata: dict[str, str] | None = None
            self.data = b""
            self.content_type = ""
            self.deleted = False
            self._exists = False

        def exists(self) -> bool:
            return self._exists

        def reload(self) -> None:
            return None

        def upload_from_string(self, data: bytes, content_type: str) -> None:
            self.data = data
            self.content_type = content_type
            self._exists = True

        def delete(self) -> None:
            self.deleted = True
            self._exists = False

    class Bucket:
        def __init__(self) -> None:
            self.name = "lab5-gemini-dev1-credit-docs"
            self.blobs: dict[str, Blob] = {}
            self.present = True

        def exists(self) -> bool:
            return self.present

        def blob(self, name: str) -> Blob:
            return self.blobs.setdefault(name, Blob())

        def list_blobs(self, prefix: str) -> list[Blob]:
            class Named(Blob):
                pass

            found: list[Blob] = []
            for name, blob in self.blobs.items():
                if name.startswith(prefix) and blob._exists:
                    blob.name = name  # type: ignore[attr-defined]
                    found.append(blob)
            return found

    class Client:
        def __init__(self, bucket: Bucket) -> None:
            self._bucket = bucket

        def bucket(self, name: str) -> Bucket:
            assert name == "lab5-gemini-dev1-credit-docs"
            return self._bucket

    bucket = Bucket()
    store = GcsObjectStore(Client(bucket), bucket.name, "credit-policies")
    store.ensure_container()
    url = store.upload_markdown("CP-RML.md", b"# policy\n", {"content_sha256": "abc"})
    assert url == "gs://lab5-gemini-dev1-credit-docs/credit-policies/CP-RML.md"
    assert store.existing_sha256("CP-RML.md") == "abc"
    assert store.list_markdown_names() == ["CP-RML.md"]
    store.delete_blob("CP-RML.md")
    assert store.list_markdown_names() == []


def test_generate_dual_write_uploads_twelve_blobs(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    rendered = run_generate(_azure_config(out), blob_store=store)
    assert len(rendered) == 12
    assert (out / "manifest.json").is_file()
    assert store.container_created
    assert store.public_access is None
    assert len(store.uploads) == 12
    for item in rendered:
        blob = store.blobs[item.document.filename]
        assert blob.data == item.markdown.encode("utf-8")
        assert blob.metadata["content_sha256"] == item.content_sha256
        assert blob.metadata["policy_id"] == item.document.id
        assert blob.metadata["policy_version"] == item.document.version
        assert "synthetic" not in blob.metadata
        assert (out / item.document.filename).is_file()


def test_hash_skip_uses_content_sha256_not_content_md5(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    config = _azure_config(out)
    first = run_generate(config, blob_store=store)
    assert len(store.uploads) == 12
    store.uploads.clear()
    run_generate(config, blob_store=store)
    assert store.uploads == []
    for blob in store.blobs.values():
        blob.content_md5 = hashlib.md5(blob.data).digest()
        blob.metadata["content_sha256"] = "0" * 64
    run_generate(config, blob_store=store)
    assert len(store.uploads) == 12
    for item in first:
        assert store.blobs[item.document.filename].metadata["content_sha256"] == (
            item.content_sha256
        )


def test_force_uploads_even_when_hash_matches(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    run_generate(_azure_config(out), blob_store=store)
    store.uploads.clear()
    run_generate(_azure_config(out, force=True), blob_store=store)
    assert len(store.uploads) == 12


def test_sync_markdown_directory_deletes_blobs_missing_locally(tmp_path: Path) -> None:
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "keep.md").write_text("# keep\n", encoding="utf-8")
    store = FakeBlobStore()
    store.blobs["keep.md"] = FakeBlob(
        data=b"stale-keep", metadata={"content_sha256": "0" * 64}
    )
    store.blobs["accepted.md"] = FakeBlob(data=b"legacy", metadata={})
    uploaded = sync_markdown_directory(
        store, directory, force=False, echo=lambda _: None
    )
    assert uploaded == 1
    assert "accepted.md" not in store.blobs
    assert "keep.md" in store.blobs
    assert "accepted.md" in store.deletes


def test_sync_empty_directory_deletes_remote_markdown(tmp_path: Path) -> None:
    directory = tmp_path / "docs"
    directory.mkdir()
    store = FakeBlobStore()
    store.blobs["stale.md"] = FakeBlob(data=b"legacy", metadata={})
    uploaded = sync_markdown_directory(
        store, directory, force=False, echo=lambda _: None
    )
    assert uploaded == 0
    assert "stale.md" not in store.blobs
    assert "stale.md" in store.deletes


def test_sync_missing_directory_does_not_sweep(tmp_path: Path) -> None:
    store = FakeBlobStore()
    store.blobs["keep.md"] = FakeBlob(data=b"x", metadata={})
    uploaded = sync_markdown_directory(
        store, tmp_path / "missing", force=False, echo=lambda _: None
    )
    assert uploaded == 0
    assert "keep.md" in store.blobs
    assert store.deletes == []


def test_azure_only_does_not_write_local(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    run_generate(_azure_config(out, gcs_only=True), blob_store=store)
    assert not out.exists()
    assert len(store.uploads) == 12


def test_local_only_does_not_open_blob_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setenv("GCS_BUCKET", "lab5-gemini-dev1-credit-docs")
    monkeypatch.setattr(
        "docgen.generate.open_blob_store",
        lambda *args, **kwargs: called.append("opened") or FakeBlobStore(),
    )
    out = tmp_path / "out"
    result = CliRunner().invoke(cli, _generate_args(out))
    assert result.exit_code == 0, result.output
    assert called == []
    assert (out / "manifest.json").is_file()


def test_dry_run_with_azure_does_not_upload(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    run_generate(_azure_config(out, dry_run=True), blob_store=store)
    assert not out.exists()
    assert store.uploads == []
    assert not store.container_created


def test_upload_failure_exits_1(tmp_path: Path) -> None:
    out = tmp_path / "credit-policies"
    store = FakeBlobStore()
    store.fail_on_upload = True
    with pytest.raises(TalosError) as exc:
        run_generate(_azure_config(out, gcs_only=True), blob_store=store)
    assert exc.value.exit_code == 1
    assert "Blob upload failed" in str(exc.value)
    assert not out.exists()


def test_cli_generate_uploads_when_azure_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeBlobStore()
    monkeypatch.setenv("GCS_BUCKET", "lab5-gemini-dev1-credit-docs")
    monkeypatch.setattr("docgen.generate.open_blob_store", lambda *a, **k: store)
    out = tmp_path / "credit-policies"
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "policy",
            "--out",
            str(out),
            "--facts",
            str(FACTS),
            "--templates",
            str(TEMPLATES),
            "--no-terraform",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(store.uploads) == 12
    assert "uploaded" in result.output
    assert (out / "manifest.json").is_file()
