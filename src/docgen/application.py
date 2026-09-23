from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from docgen.chat import GENERATE_RETRY_DELAYS
from docgen.constants import (
    APPLICATION_DISCLAIMER_PHRASES,
    APPLICATION_FILENAME_TEMPLATE,
    APPLICATION_ID_RE,
    APPLICATION_LLM_ATTEMPTS,
    APPLICATION_TYPES,
    CUSTOMER_ID_RE,
    DEFAULT_APPLICATION_CONTAINER,
    DEFAULT_APPLICATION_SCHEMA_RELATIVE,
    DEFAULT_APPLICATION_SYSTEM_PROMPT_RELATIVE,
    DEFAULT_APPLICATION_TEMPLATE_RELATIVE,
    DEFAULT_APPLICATION_USER_PROMPT_RELATIVE,
    DEFAULT_CHAT_MODEL,
    EMAIL_DOMAIN,
    FACILITY_PROMPT_HINTS,
    FORBIDDEN_OUTCOME_TOKENS,
    PRODUCT_FACILITY_LIMITS,
    PRODUCT_FAMILIES,
    PRODUCT_FAMILY_LABEL,
    PRODUCT_REQUIRED_DOCUMENTS,
    PRODUCT_REQUIRED_FACTS,
    SLOT_PRODUCT_FAMILY,
    WATERMARK,
)
from docgen.env import (
    gcs_generate_configured,
    repo_root,
    resolve_env,
    resolve_generate_env,
)
from docgen.errors import TalosError
from docgen.generate import BlobStore, first_visible_line, open_blob_store

Echo = Callable[[str], None]

REQUIRED_JSON_KEYS = (
    "application_id",
    "customer_name",
    "customer_id",
    "email",
    "phone",
    "address",
    "age_band",
    "employer",
    "annual_income",
    "product",
    "facility",
    "narrative",
    "attached_documents",
)
REQUIRED_FACILITY_KEYS = ("loan_amount",)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
NATIONAL_ID_RE = re.compile(r"\b\d{6}[- ]\d{4}\b")
CREDIT_APPLICATION_HEADING_RE = re.compile(r"^# Credit application (CA-\d{8}-\d+)$")
IDENTITY_FIELD_BY_LABEL = {
    "name": "customer_name",
    "customer id": "customer_id",
    "address": "address",
    "age band": "age_band",
    "employer": "employer",
    "annual income": "annual_income",
    "email": "email",
    "phone": "phone",
}
JUDGEMENT_LEAK_RE = re.compile(
    r"application_type|intended_outcome|expected_outcome|expected_judgement|"
    r"missing_items|"
    r"\bmissing-data\b|\bapplicationtype\b|"
    r"\baccepted\b|\brejected\b|\bapproved\b|\bdeclined\b|\bdenied\b",
    re.I,
)
FACILITY_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OPERATOR_RECORD_KEYS = (
    "application_type",
    "expected_outcome",
    "expected_judgement",
    "intended_outcome",
    "missing_items",
    "expected_policy_ids",
)


@dataclass(frozen=True)
class ApplicationGenerateConfig:
    out: Path
    facts_path: Path
    types: tuple[str, ...]
    force: bool = False
    local_only: bool = False
    dry_run: bool = False
    use_terraform: bool = True
    bucket: str = ""
    container: str = DEFAULT_APPLICATION_CONTAINER
    project: str = ""
    location: str = ""
    model: str = DEFAULT_CHAT_MODEL
    schema_path: Path | None = None
    system_prompt_path: Path | None = None
    user_prompt_path: Path | None = None
    template_path: Path | None = None
    count: int = 1
    application_id: str | None = None


@dataclass(frozen=True)
class RenderedApplication:
    application_type: str
    filename: str
    markdown: str
    content_sha256: str
    record: dict[str, Any]


class ChatCompleter(Protocol):
    def complete(self, *, messages: list[dict[str, str]]) -> str: ...


class GeminiChatCompleter:
    """JSON-mode Gemini call used to mint a synthetic borrower filing."""

    def __init__(self, rest: Any, project: str, location: str, model: str) -> None:
        self._rest = rest
        self._project = project
        self._location = location
        self._model = model

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        from docgen.gemini import candidate_text, generate_content_url

        system = "\n\n".join(
            message["content"]
            for message in messages
            if message.get("role") == "system"
        )
        user = "\n\n".join(
            message["content"] for message in messages if message.get("role") == "user"
        )
        url = generate_content_url(self._project, self._location, self._model)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        delays = iter(GENERATE_RETRY_DELAYS)
        try:
            while True:
                response = self._rest.request(
                    "POST", url, json_body=body, timeout=120.0
                )
                if response.status_code != 429:
                    break
                try:
                    delay = next(delays)
                except StopIteration:
                    break
                time.sleep(delay)
        except TalosError as exc:
            raise TalosError(f"LLM call failed: {exc}", exit_code=1) from exc
        if response.status_code >= 400:
            raise TalosError(
                f"LLM call failed: HTTP {response.status_code} {response.text}",
                exit_code=1,
            )
        payload = response.json if isinstance(response.json, dict) else {}
        content = candidate_text(payload)
        if not content.strip():
            raise TalosError("LLM call failed: empty content", exit_code=1)
        return content


def application_filename(application_id: str) -> str:
    if not re.fullmatch(APPLICATION_ID_RE, application_id):
        raise TalosError(
            f"application_id {application_id!r} is not CA-YYYYMMDD-unix_ms",
            exit_code=1,
        )
    return APPLICATION_FILENAME_TEMPLATE.format(application_id=application_id)


def contains_forbidden_outcome_token(value: str) -> bool:
    upper = value.upper()
    return any(token in upper for token in FORBIDDEN_OUTCOME_TOKENS)


def attached_documents_for(
    application_type: str, product_family: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required = PRODUCT_REQUIRED_DOCUMENTS[product_family]
    if application_type == "missing-data":
        return required[1:], (required[0],)
    return required, ()


def parse_facility_number(value: object) -> float | None:
    match = FACILITY_NUMBER_RE.search(str(value).replace(",", ""))
    if match is None:
        return None
    return float(match.group(1))


def facility_clears_limit(value: float, op: str, limit: float) -> bool:
    if op == "<=":
        return value <= limit
    if op == ">=":
        return value >= limit
    raise TalosError(f"unknown facility comparison {op!r}", exit_code=1)


def facility_limit_errors(
    facility: dict[str, Any],
    *,
    application_type: str,
    product_family: str,
) -> list[str]:
    checks = PRODUCT_FACILITY_LIMITS[product_family]
    evaluated: list[tuple[str, bool]] = []
    errors: list[str] = []
    required_fields = {field for field, _op, _limit in checks}
    if application_type in ("accepted", "rejected"):
        missing = [
            field for field in required_fields if facility.get(field) in (None, "")
        ]
        if missing:
            errors.append(
                f"facility missing {', '.join(sorted(missing))} for {product_family}"
            )
            return errors
    for field, op, limit in checks:
        raw = facility.get(field)
        if raw in (None, ""):
            continue
        number = parse_facility_number(raw)
        if number is None:
            errors.append(f"facility.{field} is not numeric: {raw!r}")
            continue
        evaluated.append((field, facility_clears_limit(number, op, limit)))
    if not evaluated:
        return errors
    clears = [ok for _field, ok in evaluated]
    if application_type == "accepted" and not all(clears):
        failed = [field for field, ok in evaluated if not ok]
        errors.append(f"accepted facility must clear published limits; failed {failed}")
    if application_type == "rejected" and all(clears):
        errors.append("rejected facility must breach at least one published limit")
    return errors


def completeness_errors(
    facility: dict[str, Any],
    *,
    application_type: str,
    product_family: str,
) -> list[str]:
    """accepted/rejected filings must include every policy fact the agent checks."""
    if application_type == "missing-data":
        return []
    errors: list[str] = []
    for field in PRODUCT_REQUIRED_FACTS[product_family]:
        raw = facility.get(field)
        if raw in (None, ""):
            errors.append(
                f"facility.{field} is required for a complete {application_type} file"
            )
            continue
        text = str(raw).strip()
        if field.endswith("_date") and not ISO_DATE_RE.fullmatch(text):
            errors.append(f"facility.{field} must be YYYY-MM-DD, got {raw!r}")
        if field == "licensed_appraiser" and text.lower() not in {"yes", "no"}:
            errors.append("facility.licensed_appraiser must be yes or no")
    if (
        application_type == "accepted"
        and product_family == "residential_mortgage"
        and not errors
    ):
        ltv = parse_facility_number(facility.get("ltv"))
        loan = parse_facility_number(facility.get("loan_amount"))
        avm_allowed = (
            ltv is not None and ltv <= 60.0 and loan is not None and loan <= 400_000.0
        )
        licensed = str(facility.get("licensed_appraiser") or "").strip().lower()
        if not avm_allowed and licensed != "yes":
            errors.append(
                "accepted residential above the AVM threshold needs licensed_appraiser yes"
            )
    return errors


def parse_llm_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise TalosError(f"LLM JSON parse failed: {exc}", exit_code=1) from exc
    if not isinstance(data, dict):
        raise TalosError("LLM JSON root must be an object", exit_code=1)
    return data


def credit_application_heading(application_id: str) -> str:
    return f"# Credit application {application_id}"


def disclaimer_phrase_in(text: str) -> str | None:
    folded = text.casefold()
    for phrase in APPLICATION_DISCLAIMER_PHRASES:
        if phrase.casefold() in folded:
            return phrase
    return None


def strip_watermark_from_facts_yaml(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if re.match(r"^watermark:\s*", line) and WATERMARK in line:
            continue
        kept.append(line)
    return "".join(kept)


def _preamble_after_heading(text: str) -> str:
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("# Credit application "):
            start = index
            break
    if start is None:
        return ""
    between: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if stripped:
            between.append(stripped)
    return "\n".join(between)


def _yaml_prefix_present(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# Credit application "):
            return False
        if stripped == "---":
            return True
    return False


def _markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().casefold()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _parse_labeled_bullets(block: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or ":" not in stripped:
            continue
        key, value = stripped[2:].split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _parse_plain_bullets(block: str) -> list[str]:
    return [
        line.strip()[2:].strip()
        for line in block.splitlines()
        if line.strip().startswith("- ")
    ]


def parse_application_markdown(text: str) -> dict[str, Any]:
    first = first_visible_line(text)
    if first == WATERMARK:
        raise TalosError(
            "application markdown must not start with the policy watermark",
            exit_code=1,
        )
    found = disclaimer_phrase_in(text)
    if found is not None:
        raise TalosError(
            f"application markdown must not contain {found!r}",
            exit_code=1,
        )
    if _yaml_prefix_present(text):
        raise TalosError(
            "application markdown must not include YAML front matter",
            exit_code=1,
        )
    heading = CREDIT_APPLICATION_HEADING_RE.fullmatch(first)
    if heading is None:
        raise TalosError(
            "first visible line must be '# Credit application CA-YYYYMMDD-unix_ms', "
            f"got {first!r}",
            exit_code=1,
        )
    sections = _markdown_sections(text)
    identity_labels = _parse_labeled_bullets(sections.get("identity", ""))
    identity = {
        IDENTITY_FIELD_BY_LABEL[label.casefold()]: value
        for label, value in identity_labels.items()
        if label.casefold() in IDENTITY_FIELD_BY_LABEL
    }
    return {
        "application_id": heading.group(1),
        "customer_name": identity.get("customer_name", ""),
        "customer_id": identity.get("customer_id", ""),
        "email": identity.get("email", ""),
        "phone": identity.get("phone", ""),
        "address": identity.get("address", ""),
        "age_band": identity.get("age_band", ""),
        "employer": identity.get("employer", ""),
        "annual_income": identity.get("annual_income", ""),
        "product": sections.get("product", "").strip(),
        "facility": _parse_labeled_bullets(sections.get("amount and financials", "")),
        "attached_documents": _parse_plain_bullets(
            sections.get("attached documents", "")
        ),
        "narrative": sections.get("applicant statement", "").strip(),
    }


def read_application_manifest(out: Path) -> dict[str, Any]:
    path = out / "manifest.json"
    if not path.is_file():
        return {"documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TalosError(
            f"invalid application manifest {path}: {exc}", exit_code=1
        ) from exc
    if not isinstance(data, dict):
        raise TalosError(
            f"application manifest {path} root must be a mapping", exit_code=1
        )
    documents = data.get("documents")
    if not isinstance(documents, (list, dict)):
        data = dict(data)
        data["documents"] = {}
    return data


def iter_manifest_documents(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    documents = manifest.get("documents")
    if isinstance(documents, dict):
        rows: list[dict[str, Any]] = []
        for key, item in documents.items():
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("application_id", key)
            rows.append(row)
        return rows
    if isinstance(documents, list):
        return [item for item in documents if isinstance(item, dict)]
    return []


def seed_application_fixtures(dest: Path, source: Path) -> int:
    """Copy committed fixture markdown into dest and merge manifest rows.

    Generated files already in dest are kept. Fixture rows win on the same
    `application_id`. Returns the number of markdown files written.
    """
    if not source.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    source_manifest = read_application_manifest(source)
    dest_data = read_application_manifest(dest)
    dest_docs = dest_data.get("documents")
    dest_map: dict[str, dict[str, Any]]
    if isinstance(dest_docs, list):
        dest_map = {}
        for item in dest_docs:
            if isinstance(item, dict) and item.get("application_id"):
                dest_map[str(item["application_id"])] = item
    elif isinstance(dest_docs, dict):
        dest_map = {
            str(key): dict(value)
            for key, value in dest_docs.items()
            if isinstance(value, dict)
        }
    else:
        dest_map = {}
    copied = 0
    for item in iter_manifest_documents(source_manifest):
        filename = str(item.get("filename") or "")
        application_id = str(item.get("application_id") or "")
        if not filename or not application_id:
            continue
        src_path = source / filename
        if not src_path.is_file():
            continue
        dest_path = dest / filename
        data = src_path.read_bytes()
        if not dest_path.is_file() or dest_path.read_bytes() != data:
            dest_path.write_bytes(data)
            copied += 1
        dest_map[application_id] = dict(item)
    dest_data = dict(dest_data)
    dest_data["documents"] = dest_map
    dest_data.pop("watermark", None)
    dest_data.setdefault("container", DEFAULT_APPLICATION_CONTAINER)
    (dest / "manifest.json").write_text(
        json.dumps(dest_data, indent=2) + "\n", encoding="utf-8"
    )
    return copied


def filename_application_id(name: str) -> str | None:
    prefix = "credit-application-"
    suffix = ".md"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    candidate = name[len(prefix) : -len(suffix)]
    if re.fullmatch(APPLICATION_ID_RE, candidate):
        return candidate
    return None


def load_existing_applications(out: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    manifest = read_application_manifest(out)
    for item in iter_manifest_documents(manifest):
        application_id = str(item.get("application_id") or "")
        filename = str(item.get("filename") or "")
        if not filename and re.fullmatch(APPLICATION_ID_RE, application_id):
            filename = application_filename(application_id)
        path = out / filename if filename else None
        record = dict(item)
        if path is not None and path.is_file():
            try:
                parsed = parse_application_markdown(path.read_text(encoding="utf-8"))
                record = {**parsed, **item}
            except TalosError:
                pass
        intended = str(item.get("intended_outcome") or item.get("slot") or "")
        record["intended_outcome"] = intended
        record["application_type"] = intended
        if filename:
            record["_filename"] = filename
        if application_id:
            records[application_id] = record
    if out.is_dir():
        for path in out.glob("credit-application-*.md"):
            application_id = filename_application_id(path.name)
            if not application_id or application_id in records:
                continue
            try:
                record = parse_application_markdown(path.read_text(encoding="utf-8"))
            except TalosError:
                continue
            record["_filename"] = path.name
            records[application_id] = record
    return records


def identity_keys(record: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    name = str(record.get("customer_name") or "").strip()
    if name:
        keys.add(name.casefold())
    for field in ("customer_id", "application_id"):
        value = str(record.get(field) or "").strip()
        if value:
            keys.add(value)
    email = str(record.get("email") or "").strip()
    if email:
        keys.add(email.casefold())
    return keys


def used_identities(
    existing: dict[str, dict[str, Any]],
    *,
    replacing: set[str] | tuple[str, ...] = (),
    pending: list[dict[str, Any]] | None = None,
) -> set[str]:
    used: set[str] = set()
    skip = set(replacing)
    for key, record in existing.items():
        if key in skip or str(record.get("application_id") or "") in skip:
            continue
        used |= identity_keys(record)
    for record in pending or []:
        used |= identity_keys(record)
    return used


def collect_used_serials(out: Path, existing: dict[str, dict[str, Any]]) -> set[str]:
    used = set(existing)
    for record in existing.values():
        application_id = str(record.get("application_id") or "")
        if application_id:
            used.add(application_id)
        filename = str(record.get("_filename") or record.get("filename") or "")
        from_name = filename_application_id(filename)
        if from_name:
            used.add(from_name)
    if out.is_dir():
        for path in out.glob("credit-application-*.md"):
            from_name = filename_application_id(path.name)
            if from_name:
                used.add(from_name)
    return used


class SerialAllocator:
    """Keep the peeked timestamp; bump unix_ms only when that serial is taken."""

    def __init__(
        self,
        used: set[str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._used = set(used or ())
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def peek(self) -> str:
        return self._next_serial(consume=False)

    def mint(self, peeked: str | None = None) -> str:
        if (
            peeked
            and peeked not in self._used
            and not contains_forbidden_outcome_token(peeked)
            and re.fullmatch(APPLICATION_ID_RE, peeked)
        ):
            self._used.add(peeked)
            return peeked
        return self._next_serial(consume=True)

    def _next_serial(self, *, consume: bool) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        date = now.strftime("%Y%m%d")
        ms = int(now.timestamp() * 1000)
        while True:
            candidate = f"CA-{date}-{ms}"
            if candidate not in self._used and not contains_forbidden_outcome_token(
                candidate
            ):
                if consume:
                    self._used.add(candidate)
                return candidate
            ms += 1


def allocate_customer_id(used: set[str]) -> str:
    for _ in range(64):
        candidate = f"SYN-{secrets.randbelow(1_000_000):06d}"
        if candidate not in used:
            return candidate
    raise TalosError("could not allocate a unique SYN-###### customer_id", exit_code=1)


def validate_record(
    record: dict[str, Any],
    *,
    application_type: str,
    used: set[str],
    required_id: str,
    product_family: str | None = None,
) -> list[str]:
    errors: list[str] = []
    missing = [key for key in REQUIRED_JSON_KEYS if key not in record]
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")
        return errors

    application_id = str(record.get("application_id") or "")
    if application_id != required_id:
        errors.append(f"application_id must be {required_id!r}, got {application_id!r}")
    if not re.fullmatch(APPLICATION_ID_RE, application_id):
        errors.append(f"application_id {application_id!r} is not CA-YYYYMMDD-unix_ms")
    if contains_forbidden_outcome_token(application_id):
        errors.append(
            f"application_id {application_id!r} contains a forbidden outcome token"
        )
    customer_id = str(record.get("customer_id") or "")
    if not re.fullmatch(CUSTOMER_ID_RE, customer_id):
        errors.append(f"customer_id {customer_id!r} must match SYN-######")
    email = str(record.get("email") or "")
    if not email.lower().endswith(f"@{EMAIL_DOMAIN}"):
        errors.append(f"email must use @{EMAIL_DOMAIN}, got {email!r}")

    family = product_family or str(record.get("product_family") or "")
    if family not in PRODUCT_FAMILIES:
        errors.append(f"unknown product_family {family!r}")
        return errors
    required_docs = PRODUCT_REQUIRED_DOCUMENTS[family]
    expected_product = PRODUCT_FAMILY_LABEL[family]
    if str(record.get("product") or "").strip() != expected_product:
        errors.append(f"product must be {expected_product!r}")

    facility = record.get("facility")
    if not isinstance(facility, dict) or not facility:
        errors.append("facility must be a non-empty mapping")
    else:
        for key in REQUIRED_FACILITY_KEYS:
            if key not in facility or facility[key] in (None, ""):
                errors.append(f"facility.{key} is required")
        errors.extend(
            facility_limit_errors(
                facility,
                application_type=application_type,
                product_family=family,
            )
        )
        errors.extend(
            completeness_errors(
                facility,
                application_type=application_type,
                product_family=family,
            )
        )

    attached = record.get("attached_documents")
    if not isinstance(attached, list) or not all(
        isinstance(item, str) and item.strip() for item in attached
    ):
        errors.append("attached_documents must be a list of titles")
    else:
        attached_set = {item.strip() for item in attached}
        required_set = set(required_docs)
        extra = attached_set - required_set
        if extra:
            errors.append(f"unknown attached document titles: {sorted(extra)}")
        if application_type == "missing-data":
            if attached_set >= required_set:
                errors.append(
                    "missing-data must omit at least one required document title"
                )
        elif attached_set != required_set:
            errors.append(
                f"{application_type} must list the full required set "
                f"{sorted(required_set)}, got {sorted(attached_set)}"
            )

    for field in (
        "customer_name",
        "address",
        "phone",
        "employer",
        "product",
        "narrative",
    ):
        if not str(record.get(field) or "").strip():
            errors.append(f"{field} is required")

    blob = json.dumps(record, ensure_ascii=False)
    if SSN_RE.search(blob) or NATIONAL_ID_RE.search(blob):
        errors.append("record looks like real national-id / SSN; use fictional values")
    scan = {
        key: value
        for key, value in record.items()
        if key not in OPERATOR_RECORD_KEYS and key != "product_family"
    }
    if JUDGEMENT_LEAK_RE.search(json.dumps(scan, ensure_ascii=False)):
        errors.append(
            "filing fields must not contain ApplicationType or judgement labels"
        )
    found = disclaimer_phrase_in(json.dumps(record, ensure_ascii=False))
    if found is not None:
        errors.append(f"filing must not contain {found!r}")

    keys = identity_keys(record)
    overlap = keys & used
    if overlap:
        errors.append(f"identity not unique vs manifest: {sorted(overlap)}")
    return errors


def validate_filing_markdown(markdown: str, record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    application_id = str(record.get("application_id") or "")
    expected = credit_application_heading(application_id)
    first = first_visible_line(markdown)
    if first != expected:
        errors.append(f"first visible line must be {expected!r}, got {first!r}")
    if _yaml_prefix_present(markdown):
        errors.append("YAML front matter is forbidden")
    if JUDGEMENT_LEAK_RE.search(markdown):
        errors.append("filing must not contain ApplicationType or judgement labels")
    found = disclaimer_phrase_in(markdown)
    if found is not None:
        errors.append(f"filing must not contain {found!r}")
    if _preamble_after_heading(markdown):
        errors.append("preamble between H1 and first H2 is forbidden")
    if contains_forbidden_outcome_token(application_id):
        errors.append("application_id encodes an outcome token")
    if re.fullmatch(
        APPLICATION_ID_RE, application_id
    ) and contains_forbidden_outcome_token(application_filename(application_id)):
        errors.append("filename encodes an outcome token")
    sections = _markdown_sections(markdown)
    for heading in (
        "identity",
        "product",
        "amount and financials",
        "attached documents",
        "applicant statement",
    ):
        if heading not in sections:
            errors.append(f"customer filing is missing {heading} section")
    narrative = str(record.get("narrative") or "").strip()
    statement = sections.get("applicant statement", "").strip()
    if narrative and statement != narrative:
        errors.append("Applicant statement must hold the filing narrative")
    amount = ""
    facility = record.get("facility")
    if isinstance(facility, dict):
        amount = str(facility.get("loan_amount") or "")
    if amount and amount not in markdown:
        errors.append("customer filing is missing the requested amount")
    for title in record.get("attached_documents") or []:
        if str(title) not in markdown:
            errors.append(f"attached document title missing from filing: {title}")
    return errors


def render_application(
    record: dict[str, Any],
    template_path: Path,
    *,
    application_type: str,
) -> RenderedApplication:
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    try:
        markdown = env.get_template(template_path.name).render(app=record)
    except TemplateError as exc:
        raise TalosError(f"Failed to render application: {exc}", exit_code=1) from exc
    if not markdown.endswith("\n"):
        markdown += "\n"
    leak_errors = validate_filing_markdown(markdown, record)
    if leak_errors:
        raise TalosError(
            "rendered application is not a customer filing: " + "; ".join(leak_errors),
            exit_code=1,
        )
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    application_id = str(record["application_id"])
    return RenderedApplication(
        application_type=application_type,
        filename=application_filename(application_id),
        markdown=markdown,
        content_sha256=digest,
        record=record,
    )


def _manifest_row(
    record: dict[str, Any],
    *,
    filename: str,
    content_sha256: str | None,
    intended_outcome: str,
) -> dict[str, Any]:
    application_id = str(record.get("application_id") or "")
    return {
        "application_id": application_id,
        "intended_outcome": intended_outcome,
        "expected_outcome": str(record.get("expected_outcome") or intended_outcome),
        "filename": filename,
        "blob_path": filename,
        "content_sha256": content_sha256,
        "customer_name": record.get("customer_name"),
        "customer_id": record.get("customer_id"),
        "product_family": record.get("product_family"),
    }


def build_application_manifest(
    existing: dict[str, dict[str, Any]],
    rendered: list[RenderedApplication],
    *,
    generated_at: str,
    container: str,
) -> dict[str, Any]:
    documents: dict[str, dict[str, Any]] = {}
    for application_id, record in existing.items():
        filename = str(record.get("_filename") or record.get("filename") or "")
        if not filename and re.fullmatch(APPLICATION_ID_RE, application_id):
            filename = application_filename(application_id)
        documents[application_id] = _manifest_row(
            record,
            filename=filename,
            content_sha256=record.get("content_sha256"),
            intended_outcome=str(
                record.get("intended_outcome") or record.get("application_type") or ""
            ),
        )
    for item in rendered:
        application_id = str(item.record["application_id"])
        intended = str(item.record.get("intended_outcome") or item.application_type)
        documents[application_id] = _manifest_row(
            item.record,
            filename=item.filename,
            content_sha256=item.content_sha256,
            intended_outcome=intended,
        )
    return {
        "generated_at": generated_at,
        "container": container,
        "documents": documents,
    }


def write_applications(
    out: Path,
    rendered: list[RenderedApplication],
    manifest: dict[str, Any],
    echo: Echo,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for item in rendered:
        path = out / item.filename
        path.write_text(item.markdown, encoding="utf-8")
        echo(f"wrote {path}")
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    echo(f"wrote {manifest_path}")


def application_blob_metadata(item: RenderedApplication) -> dict[str, str]:
    return {
        "content_sha256": item.content_sha256,
        "application_id": str(item.record["application_id"]),
    }


def _plan_generate_work(
    config: ApplicationGenerateConfig,
    existing: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str | None]], set[str]]:
    if config.force:
        if config.count != 1:
            raise TalosError("--force cannot be combined with --count", exit_code=1)
        target = (config.application_id or "").strip()
        if not target:
            raise TalosError("--force requires --application-id", exit_code=1)
        if not re.fullmatch(APPLICATION_ID_RE, target):
            raise TalosError(
                f"application_id {target!r} is not CA-YYYYMMDD-unix_ms",
                exit_code=1,
            )
        previous = existing.get(target)
        if previous is None:
            raise TalosError(
                f"application {target} not found; generate without --force to append",
                exit_code=1,
            )
        if config.types:
            unknown = [kind for kind in config.types if kind not in APPLICATION_TYPES]
            if unknown:
                raise TalosError(
                    f"unknown application type {unknown[0]!r}",
                    exit_code=1,
                )
            if len(config.types) != 1:
                raise TalosError(
                    "--force --application-id accepts at most one --type",
                    exit_code=1,
                )
            kind = config.types[0]
        else:
            kind = str(
                previous.get("intended_outcome")
                or previous.get("application_type")
                or ""
            )
            if kind not in APPLICATION_TYPES:
                raise TalosError(
                    f"existing application {target} has no intended_outcome; pass --type",
                    exit_code=1,
                )
        return [(kind, target)], {target}

    types = config.types
    if not types:
        raise TalosError("exactly one of --type or --all is required", exit_code=1)
    unknown = [kind for kind in types if kind not in APPLICATION_TYPES]
    if unknown:
        raise TalosError(
            f"unknown application type {unknown[0]!r}",
            exit_code=1,
        )
    if config.count < 1:
        raise TalosError("--count must be >= 1", exit_code=1)
    work = [(kind, None) for kind in types for _ in range(config.count)]
    return work, set()


def run_generate_application(
    config: ApplicationGenerateConfig,
    echo: Echo = print,
    *,
    completer: ChatCompleter | None = None,
    blob_store: BlobStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[RenderedApplication]:
    existing = load_existing_applications(config.out)
    work, replacing = _plan_generate_work(config, existing)
    allocator = SerialAllocator(
        collect_used_serials(config.out, existing),
        clock=clock,
    )

    if config.dry_run:
        echo("dry-run: no LLM call")
        for kind, forced_id in work:
            application_id = forced_id or allocator.mint()
            echo(
                f"dry-run: would generate {kind} -> "
                f"{config.out / application_filename(application_id)}"
            )
        if not config.local_only:
            echo("dry-run: no blobs uploaded")
        return []

    env = resolve_env(use_terraform=config.use_terraform)
    project = config.project or env.get("GOOGLE_CLOUD_PROJECT") or ""
    location = config.location or env.get("GOOGLE_CLOUD_LOCATION") or ""
    if completer is None and (not project or not location):
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLOUD_PROJECT", project),
                ("GOOGLE_CLOUD_LOCATION", location),
            )
            if not value
        ]
        raise TalosError(
            "Google Cloud environment is not configured (missing "
            + ", ".join(missing)
            + "). Set the variables, run `gmake infra-create` "
            "(writes `infra/outputs.json`), or pass --dry-run.",
            exit_code=2,
        )

    want_gcs = not config.local_only
    generate_env = resolve_generate_env(use_terraform=config.use_terraform)
    store = blob_store
    if want_gcs and store is None:
        if not gcs_generate_configured(generate_env, config.bucket):
            raise TalosError(
                "Google Cloud environment is not configured (missing GCS_BUCKET). "
                "Set the variable, run `gmake infra-create`, or pass --local-only.",
                exit_code=2,
            )

    chat = completer or _default_completer(project, location, config.model)
    paths = _prompt_paths(config)
    system_prompt = paths.system.read_text(encoding="utf-8")
    schema_text = paths.schema.read_text(encoding="utf-8")
    facts_text = strip_watermark_from_facts_yaml(
        config.facts_path.read_text(encoding="utf-8")
    )
    user_template = _jinja_env(paths.user.parent).get_template(paths.user.name)

    rendered: list[RenderedApplication] = []
    pending_records: list[dict[str, Any]] = []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = existing.get(next(iter(replacing), ""), {}) if replacing else {}

    for kind, forced_id in work:
        used = used_identities(existing, replacing=replacing, pending=pending_records)
        customer_id = allocate_customer_id(used)
        product_family = SLOT_PRODUCT_FAMILY[kind]
        attached, _omitted = attached_documents_for(kind, product_family)
        candidate = forced_id or allocator.peek()
        record, item = _complete_one(
            chat,
            user_template=user_template,
            system_prompt=system_prompt,
            schema_text=schema_text,
            facts_text=facts_text,
            application_type=kind,
            application_id=candidate,
            customer_id=customer_id,
            product_family=product_family,
            attached_documents=attached,
            used=used,
            echo=echo,
            template_path=paths.template,
        )
        application_id = forced_id or allocator.mint(peeked=candidate)
        intended = kind
        if forced_id and not config.types:
            intended = str(
                previous.get("expected_outcome")
                or previous.get("intended_outcome")
                or kind
            )
        record["intended_outcome"] = intended
        record["expected_outcome"] = intended
        record["_filename"] = item.filename
        record["content_sha256"] = item.content_sha256
        existing[application_id] = record
        pending_records.append(record)
        rendered.append(item)
        manifest = build_application_manifest(
            existing,
            [],
            generated_at=generated_at,
            container=config.container,
        )
        write_applications(config.out, [item], manifest, echo)
        if want_gcs:
            if store is None:
                store = open_blob_store(
                    generate_env,
                    container=config.container,
                    bucket=config.bucket,
                )
            _upload_applications(store, [item], echo=echo)

    return rendered


def _complete_one(
    chat: ChatCompleter,
    *,
    user_template: Any,
    system_prompt: str,
    schema_text: str,
    facts_text: str,
    application_type: str,
    application_id: str,
    customer_id: str,
    product_family: str,
    attached_documents: tuple[str, ...],
    used: set[str],
    echo: Echo,
    template_path: Path,
) -> tuple[dict[str, Any], RenderedApplication]:
    last_errors: list[str] = []
    product_label = PRODUCT_FAMILY_LABEL[product_family]
    for attempt in range(1, APPLICATION_LLM_ATTEMPTS + 1):
        user = user_template.render(
            application_type=application_type,
            application_id=application_id,
            customer_id=customer_id,
            product_family=product_family,
            product_label=product_label,
            attached_documents=attached_documents,
            schema=schema_text,
            facts_yaml=facts_text,
            facility_hint=FACILITY_PROMPT_HINTS[product_family],
            used_identities=sorted(used),
            previous_errors=last_errors,
        )
        echo(f"generating {application_type} ({application_id}) attempt {attempt}")
        try:
            raw = chat.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user},
                ]
            )
            record = parse_llm_json(raw)
            record["application_id"] = application_id
            record["customer_id"] = str(record.get("customer_id") or customer_id)
            record["product_family"] = product_family
            record["product"] = product_label
            record["attached_documents"] = list(attached_documents)
            expected = str(
                record.get("expected_outcome") or record.get("intended_outcome") or ""
            )
            if expected != application_type:
                errors = [
                    f"expected_outcome must be {application_type!r}, got {expected!r}"
                ]
            else:
                for leak_key in OPERATOR_RECORD_KEYS:
                    record.pop(leak_key, None)
                errors = validate_record(
                    record,
                    application_type=application_type,
                    used=used,
                    required_id=application_id,
                    product_family=product_family,
                )
                if not errors:
                    record["expected_outcome"] = application_type
                    record["intended_outcome"] = application_type
        except TalosError as exc:
            errors = [str(exc)]
            record = {}
        if not errors:
            try:
                return record, render_application(
                    record, template_path, application_type=application_type
                )
            except TalosError as exc:
                errors = [str(exc)]
        last_errors = errors
        echo(f"validation failed: {'; '.join(errors)}")
    raise TalosError(
        "application generation failed after retry:\n- " + "\n- ".join(last_errors),
        exit_code=1,
    )


def _upload_applications(
    store: BlobStore,
    rendered: list[RenderedApplication],
    *,
    echo: Echo,
) -> None:
    try:
        store.ensure_container()
        for item in rendered:
            url = store.upload_markdown(
                item.filename,
                item.markdown.encode("utf-8"),
                application_blob_metadata(item),
            )
            echo(
                f"{item.record['application_id']}  {item.filename}  blob={url}  uploaded"
            )
    except TalosError:
        raise
    except Exception as exc:
        raise TalosError(f"Blob upload failed: {exc}", exit_code=1) from exc


@dataclass(frozen=True)
class _PromptPaths:
    schema: Path
    system: Path
    user: Path
    template: Path


def _prompt_paths(config: ApplicationGenerateConfig) -> _PromptPaths:
    root = repo_root()
    return _PromptPaths(
        schema=config.schema_path or (root / DEFAULT_APPLICATION_SCHEMA_RELATIVE),
        system=config.system_prompt_path
        or (root / DEFAULT_APPLICATION_SYSTEM_PROMPT_RELATIVE),
        user=config.user_prompt_path
        or (root / DEFAULT_APPLICATION_USER_PROMPT_RELATIVE),
        template=config.template_path or (root / DEFAULT_APPLICATION_TEMPLATE_RELATIVE),
    )


def _jinja_env(directory: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(directory)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _default_completer(project: str, location: str, model: str) -> GeminiChatCompleter:
    from docgen.rest import RequestsRest

    return GeminiChatCompleter(RequestsRest(), project, location, model)
