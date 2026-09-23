from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateError,
    TemplateNotFound,
)

from docgen.constants import (
    CORPUS_IDS,
    DEFAULT_CONTAINER,
    WATERMARK,
)
from docgen.env import gcs_generate_configured, resolve_generate_env
from docgen.errors import TalosError

MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"

Echo = Callable[[str], None]

REQUIRED_DOC_FIELDS = (
    "id",
    "title",
    "filename",
    "topics",
    "version",
    "effective_date",
    "facts",
)


@dataclass(frozen=True)
class PolicyDocument:
    id: str
    title: str
    filename: str
    topics: tuple[str, ...]
    version: str
    effective_date: str
    facts: dict[str, str]


@dataclass(frozen=True)
class RenderedDocument:
    document: PolicyDocument
    markdown: str
    content_sha256: str


@dataclass(frozen=True)
class GenerateConfig:
    out: Path
    facts_path: Path
    templates_dir: Path
    container: str = DEFAULT_CONTAINER
    bucket: str = ""
    local_only: bool = False
    gcs_only: bool = False
    dry_run: bool = False
    force: bool = False
    fail_if_missing_gcs: bool = False
    use_terraform: bool = True


class BlobStore(Protocol):
    def ensure_container(self) -> None: ...

    def existing_sha256(self, blob_name: str) -> str | None: ...

    def blob_url(self, blob_name: str) -> str: ...

    def upload_markdown(
        self, blob_name: str, data: bytes, metadata: dict[str, str]
    ) -> str: ...

    def list_markdown_names(self) -> list[str]: ...

    def delete_blob(self, blob_name: str) -> None: ...


class GcsObjectStore:
    """Object store for one prefix inside a Terraform-managed bucket.

    `container` is the prefix (`credit-policies` or `client-applications`).
    The bucket itself is created by Terraform; this class does not create it.
    """

    def __init__(self, client: Any, bucket: str, container: str) -> None:
        self._bucket = client.bucket(bucket)
        self._container = container.strip("/")

    def _blob(self, name: str) -> Any:
        return self._bucket.blob(f"{self._container}/{name}")

    def ensure_container(self) -> None:
        if not self._bucket.exists():
            raise TalosError(
                f"GCS bucket gs://{self._bucket.name} does not exist. "
                "Run `gmake terraform-apply`.",
                exit_code=2,
            )

    def existing_sha256(self, blob_name: str) -> str | None:
        blob = self._blob(blob_name)
        if not blob.exists():
            return None
        blob.reload()
        metadata = blob.metadata or {}
        digest = metadata.get("content_sha256") or ""
        return digest.lower() or None

    def blob_url(self, blob_name: str) -> str:
        return f"gs://{self._bucket.name}/{self._container}/{blob_name}"

    def upload_markdown(
        self, blob_name: str, data: bytes, metadata: dict[str, str]
    ) -> str:
        blob = self._blob(blob_name)
        blob.metadata = dict(metadata)
        blob.upload_from_string(data, content_type=MARKDOWN_CONTENT_TYPE)
        return self.blob_url(blob_name)

    def list_markdown_names(self) -> list[str]:
        prefix = f"{self._container}/"
        names: list[str] = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            name = str(blob.name)
            if not name.endswith(".md"):
                continue
            names.append(name.removeprefix(prefix))
        return names

    def delete_blob(self, blob_name: str) -> None:
        blob = self._blob(blob_name)
        if blob.exists():
            blob.delete()


def resolve_bucket(
    env: dict[str, str],
    bucket: str = "",
    *,
    purpose: str = "generate",
) -> str:
    name = bucket or env.get("GCS_BUCKET") or ""
    if name:
        return name
    if purpose == "deploy":
        raise TalosError(
            "Google Cloud environment is not configured (missing GCS_BUCKET). "
            "Set the variable or run `gmake terraform-apply` (writes `infra/outputs.json`).",
            exit_code=2,
        )
    raise TalosError(
        "Google Cloud environment is not configured (missing GCS_BUCKET). "
        "Set the variable, run `gmake terraform-apply` (writes `infra/outputs.json`), "
        "or pass --bucket / --local-only.",
        exit_code=2,
    )


def open_blob_store(
    env: dict[str, str],
    *,
    container: str,
    bucket: str = "",
    purpose: str = "generate",
) -> BlobStore:
    name = resolve_bucket(env, bucket, purpose=purpose)
    from google.cloud import storage

    project = env.get("GOOGLE_CLOUD_PROJECT") or None
    return GcsObjectStore(storage.Client(project=project), name, container)


def blob_metadata(item: RenderedDocument) -> dict[str, str]:
    return {
        "content_sha256": item.content_sha256,
        "policy_id": item.document.id,
        "policy_version": item.document.version,
    }


def sync_markdown_directory(
    store: BlobStore,
    directory: Path,
    *,
    force: bool,
    echo: Echo,
) -> int:
    """Upload `*.md` from directory. Skip when blob metadata content_sha256 matches."""
    uploaded = 0
    try:
        store.ensure_container()
        if not directory.is_dir():
            echo(f"blob-sync: local directory missing {directory}")
            return 0
        paths = sorted(directory.glob("*.md"))
        if not paths:
            echo(f"blob-sync: no markdown in {directory}")
        for path in paths:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            existing = None if force else store.existing_sha256(path.name)
            if existing is not None and existing.lower() == digest:
                echo(f"{path.name}  blob={store.blob_url(path.name)}  skipped")
                continue
            metadata = {"content_sha256": digest}
            url = store.upload_markdown(path.name, data, metadata)
            echo(f"{path.name}  blob={url}  uploaded")
            uploaded += 1
        keep = {path.name for path in paths}
        for name in store.list_markdown_names():
            if name in keep:
                continue
            store.delete_blob(name)
            echo(f"{name}  blob deleted")
    except TalosError:
        raise
    except Exception as exc:
        raise TalosError(f"Blob upload failed: {exc}", exit_code=1) from exc
    return uploaded


def upload_blobs(
    store: BlobStore,
    rendered: list[RenderedDocument],
    *,
    force: bool,
    local_dir: Path | None,
    echo: Echo,
) -> None:
    try:
        store.ensure_container()
        for item in rendered:
            name = item.document.filename
            local = str(local_dir / name) if local_dir is not None else "-"
            existing = None if force else store.existing_sha256(name)
            if existing is not None and existing.lower() == item.content_sha256:
                url = store.blob_url(name)
                echo(f"{item.document.id}  {name}  local={local}  blob={url}  skipped")
                continue
            url = store.upload_markdown(
                name, item.markdown.encode("utf-8"), blob_metadata(item)
            )
            echo(f"{item.document.id}  {name}  local={local}  blob={url}  uploaded")
    except TalosError:
        raise
    except Exception as exc:
        raise TalosError(f"Blob upload failed: {exc}", exit_code=1) from exc


def run_generate(
    config: GenerateConfig,
    echo: Echo = print,
    *,
    blob_store: BlobStore | None = None,
) -> list[RenderedDocument]:
    if config.local_only and config.gcs_only:
        raise TalosError(
            "--local-only and --gcs-only are mutually exclusive",
            exit_code=1,
        )

    documents = load_and_validate_facts(config.facts_path)
    rendered = render_documents(documents, config.templates_dir)
    _validate_renders(rendered)

    want_local = not config.gcs_only
    want_gcs = not config.local_only
    env = resolve_generate_env(use_terraform=config.use_terraform)
    store = blob_store
    if want_gcs and store is None:
        if not gcs_generate_configured(env, config.bucket):
            if config.fail_if_missing_gcs or config.gcs_only:
                raise TalosError(
                    "Google Cloud environment is not configured (missing GCS_BUCKET). "
                    "Set the variable, run `gmake terraform-apply` (writes `infra/outputs.json`), "
                    "or pass --bucket / --local-only.",
                    exit_code=2,
                )
            echo("Google Cloud not configured; writing local files only")
            want_gcs = False

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(
        rendered, generated_at=generated_at, container=config.container
    )

    if config.dry_run:
        echo("dry-run: no files written")
        for item in rendered:
            echo(
                f"{item.document.id}  {item.document.filename}  "
                f"sha256={item.content_sha256}"
            )
        if want_gcs:
            echo("dry-run: no blobs uploaded")
        return rendered

    if want_gcs and store is None:
        store = open_blob_store(env, container=config.container, bucket=config.bucket)

    if want_local:
        write_local(config.out, rendered, manifest, echo)
    if want_gcs:
        if store is None:
            raise TalosError("Blob store is not configured", exit_code=1)
        upload_blobs(
            store,
            rendered,
            force=config.force,
            local_dir=config.out if want_local else None,
            echo=echo,
        )

    return rendered


def load_and_validate_facts(path: Path) -> list[PolicyDocument]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TalosError(f"Cannot read facts file {path}: {exc}", exit_code=1) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TalosError(f"Invalid YAML in {path}: {exc}", exit_code=3) from exc
    if not isinstance(data, dict):
        raise TalosError("facts.yaml root must be a mapping", exit_code=3)

    errors: list[str] = []
    if "watermark" in data:
        errors.append("facts.yaml must not set watermark")

    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise TalosError(
            "facts.yaml must contain a non-empty documents list", exit_code=3
        )

    seen_ids: set[str] = set()
    seen_keys: dict[str, str] = {}
    parsed: list[PolicyDocument] = []
    for i, item in enumerate(documents):
        parsed_doc = _parse_document(i, item, seen_ids, seen_keys, errors)
        if parsed_doc is not None:
            parsed.append(parsed_doc)

    ids = [doc.id for doc in parsed]
    missing_ids = [doc_id for doc_id in CORPUS_IDS if doc_id not in ids]
    extra_ids = [doc_id for doc_id in ids if doc_id not in CORPUS_IDS]
    if missing_ids or extra_ids:
        errors.append(
            "document ids must be the 12 corpus ids"
            + (f"; missing {missing_ids}" if missing_ids else "")
            + (f"; extra {extra_ids}" if extra_ids else "")
        )

    if errors:
        raise TalosError(
            "facts validation failed:\n- " + "\n- ".join(errors),
            exit_code=3,
        )
    return parsed


def render_documents(
    documents: list[PolicyDocument], templates_dir: Path
) -> list[RenderedDocument]:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return [_render_one(env, doc) for doc in documents]


def build_manifest(
    rendered: list[RenderedDocument],
    *,
    generated_at: str,
    container: str,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "container": container,
        "documents": [
            {
                "id": item.document.id,
                "title": item.document.title,
                "filename": item.document.filename,
                "blob_path": item.document.filename,
                "content_sha256": item.content_sha256,
                "topics": list(item.document.topics),
                "version": item.document.version,
                "effective_date": item.document.effective_date,
            }
            for item in rendered
        ],
    }


def write_local(
    out: Path,
    rendered: list[RenderedDocument],
    manifest: dict[str, Any],
    echo: Echo,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for item in rendered:
        path = out / item.document.filename
        path.write_text(item.markdown, encoding="utf-8")
        echo(f"wrote {path}")
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    echo(f"wrote {manifest_path}")


def first_visible_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _parse_document(
    index: int,
    item: Any,
    seen_ids: set[str],
    seen_keys: dict[str, str],
    errors: list[str],
) -> PolicyDocument | None:
    if not isinstance(item, dict):
        errors.append(f"documents[{index}] must be a mapping")
        return None
    missing = [key for key in REQUIRED_DOC_FIELDS if key not in item]
    if missing:
        errors.append(f"documents[{index}] missing {', '.join(missing)}")
        return None

    doc_id = str(item["id"])
    if doc_id in seen_ids:
        errors.append(f"duplicate document id {doc_id}")
    seen_ids.add(doc_id)

    filename = str(item["filename"])
    if not re.fullmatch(re.escape(doc_id) + r"-.+\.md", filename):
        errors.append(f"{doc_id}: filename {filename!r} must match ^{doc_id}-.+\\.md$")

    topics = item["topics"]
    if not isinstance(topics, list) or not all(
        isinstance(topic, str) for topic in topics
    ):
        errors.append(f"{doc_id}: topics must be a list of strings")
        topics = []

    facts = item["facts"]
    if not isinstance(facts, dict) or not facts:
        errors.append(f"{doc_id}: facts must be a non-empty mapping")
        facts = {}

    str_facts: dict[str, str] = {}
    for key, value in facts.items():
        key_s = str(key)
        if key_s in seen_keys:
            errors.append(
                f"duplicate fact key {key_s!r} ({seen_keys[key_s]} and {doc_id})"
            )
        seen_keys[key_s] = doc_id
        if value is None:
            errors.append(f"{doc_id}.{key_s} is null")
            continue
        str_facts[key_s] = value if isinstance(value, str) else str(value)

    return PolicyDocument(
        id=doc_id,
        title=str(item["title"]),
        filename=filename,
        topics=tuple(topics),
        version=str(item["version"]),
        effective_date=str(item["effective_date"]),
        facts=str_facts,
    )


def _render_one(env: Environment, doc: PolicyDocument) -> RenderedDocument:
    template_name = f"{doc.filename}.j2"
    try:
        template = env.get_template(template_name)
        markdown = template.render(doc=doc, facts=doc.facts)
    except TemplateNotFound as exc:
        raise TalosError(
            f"Missing template {template_name}",
            exit_code=1,
        ) from exc
    except TemplateError as exc:
        raise TalosError(f"Failed to render {doc.id}: {exc}", exit_code=1) from exc
    if not markdown.endswith("\n"):
        markdown += "\n"
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return RenderedDocument(document=doc, markdown=markdown, content_sha256=digest)


def _validate_renders(rendered: list[RenderedDocument]) -> None:
    errors: list[str] = []
    for item in rendered:
        text = item.markdown
        if not text.strip():
            errors.append(f"{item.document.id}: empty document")
            continue
        first = first_visible_line(text)
        expected_header = (
            f"> Policy ID: {item.document.id} | Version: {item.document.version} "
            f"| Effective: {item.document.effective_date}"
        )
        if first != expected_header:
            errors.append(
                f"{item.document.id}: first visible line must be {expected_header!r}, got {first!r}"
            )
        if WATERMARK in text or "Not a real bank policy" in text:
            errors.append(f"{item.document.id}: must not contain a synthetic warning")
        for key, value in item.document.facts.items():
            if value not in text:
                errors.append(
                    f"{item.document.id}: fact {key} value {value!r} missing from render"
                )
    if errors:
        raise TalosError(
            "render validation failed:\n- " + "\n- ".join(errors),
            exit_code=1,
        )
