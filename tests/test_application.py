from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from docgen.application import (
    ApplicationGenerateConfig,
    SerialAllocator,
    application_filename,
    attached_documents_for,
    contains_forbidden_outcome_token,
    credit_application_heading,
    disclaimer_phrase_in,
    parse_application_markdown,
    parse_llm_json,
    run_generate_application,
    seed_application_fixtures,
    strip_watermark_from_facts_yaml,
    validate_filing_markdown,
    validate_record,
)
from docgen.cli import cli
from docgen.constants import (
    APPLICATION_DISCLAIMER_PHRASES,
    APPLICATION_ID_RE,
    APPLICATION_TYPES,
    FORBIDDEN_OUTCOME_TOKENS,
    PRODUCT_FAMILIES,
    PRODUCT_FAMILY_LABEL,
    PRODUCT_REQUIRED_DOCUMENTS,
    SLOT_PRODUCT_FAMILY,
    WATERMARK,
)
from docgen.env import repo_root
from docgen.errors import TalosError
from docgen.generate import first_visible_line
from tests.fakes import FakeBlob, FakeBlobStore
from tests.helpers import load_application_fixtures

pytestmark = pytest.mark.unit

FACTS = repo_root() / "corpus/facts.yaml"
FIXTURES = repo_root() / "tests/fixtures/client-applications"
FROZEN_NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
FROZEN_SERIAL = "CA-20260914-1789344000000"
SAMPLE_SERIAL = "CA-20260115-1768478400000"


def _frozen_clock() -> datetime:
    return FROZEN_NOW


class ScriptedCompleter:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if not self.payloads:
            raise AssertionError("unexpected LLM call")
        payload = self.payloads.pop(0)
        return json.dumps(payload)


def _valid_record(kind: str, **overrides: object) -> dict:
    family = SLOT_PRODUCT_FAMILY[kind]
    attached, _omitted = attached_documents_for(kind, family)
    names = {
        "accepted": ("Pat Rivet", "SYN-111111", "pat.rivet@example.invalid"),
        "rejected": ("Pat Quarry", "SYN-222222", "pat.quarry@example.invalid"),
        "missing-data": ("Pat Harbor", "SYN-333333", "pat.harbor@example.invalid"),
    }
    customer_name, customer_id, email = names[kind]
    record: dict = {
        "application_id": SAMPLE_SERIAL,
        "customer_name": customer_name,
        "customer_id": customer_id,
        "email": email,
        "phone": "+1-555-0100",
        "address": "1 Demo Street, Contoso City, CD 00000",
        "age_band": "35-44",
        "employer": "Northwind",
        "annual_income": "USD 90,000",
        "product": PRODUCT_FAMILY_LABEL[family],
        "product_family": family,
        "facility": {
            "accepted": {
                "loan_amount": "USD 320,000",
                "property_value": "USD 450,000",
                "ltv": "71%",
                "dti": "36%",
                "credit_score": "720",
                "occupancy": "owner-occupied",
                "appraisal_date": "2026-08-01",
                "licensed_appraiser": "yes",
            },
            "rejected": {
                "loan_amount": "USD 3,600,000",
                "property_value": "USD 5,000,000",
                "ltv": "72%",
                "dscr": "1.10x",
                "valuation_date": "2026-06-15",
            },
            "missing-data": {
                "loan_amount": "USD 400,000",
                "years_in_operation": "5 years",
                "tenor_months": "12",
            },
        }[kind],
        "narrative": "I request this facility and list the documents I am submitting.",
        "attached_documents": list(attached),
        "expected_outcome": kind,
    }
    record.update(overrides)
    return record


def _unique_record(kind: str, index: int, **overrides: object) -> dict:
    values: dict[str, object] = {
        "customer_name": f"Pat Case {index}",
        "customer_id": f"SYN-{index:06d}",
        "email": f"pat.case.{index}@example.invalid",
    }
    values.update(overrides)
    return _valid_record(kind, **values)


def _config(out: Path, **overrides: object) -> ApplicationGenerateConfig:
    values: dict[str, object] = dict(
        out=out,
        facts_path=FACTS,
        types=("accepted",),
        local_only=True,
        use_terraform=False,
        project="lab5-gemini-dev1",
        location="us-east1",
    )
    values.update(overrides)
    return ApplicationGenerateConfig(**values)  # type: ignore[arg-type]


def test_application_help_documents_flags() -> None:
    result = CliRunner().invoke(cli, ["generate", "application", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--type",
        "--all",
        "--count",
        "--force",
        "--application-id",
        "--local-only",
        "--dry-run",
    ):
        assert flag in result.output
    assert "knowledge source" in result.output.lower() or "PUT" in result.output
    assert "operator" in result.output.lower()


def test_application_requires_type_or_all() -> None:
    result = CliRunner().invoke(
        cli, ["generate", "application", "--local-only", "--no-terraform"]
    )
    assert result.exit_code == 1
    assert "--type" in result.output and "--all" in result.output


def test_application_dry_run_does_not_call_llm_or_write(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    completer = ScriptedCompleter([_valid_record("accepted")])
    rendered = run_generate_application(
        _config(out, dry_run=True),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert rendered == []
    assert completer.calls == []
    assert not out.exists()


def test_application_cli_dry_run_exit_0(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "application",
            "--type",
            "accepted",
            "--dry-run",
            "--local-only",
            "--out",
            str(tmp_path / "apps"),
            "--no-terraform",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "no LLM call" in result.output
    assert "credit-application-CA-" in result.output


def test_generate_one_slot_writes_opaque_customer_filing(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    completer = ScriptedCompleter([_valid_record("accepted")])
    rendered = run_generate_application(
        _config(out),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert len(rendered) == 1
    application_id = rendered[0].record["application_id"]
    assert application_id == FROZEN_SERIAL
    assert re.fullmatch(APPLICATION_ID_RE, application_id)
    assert not contains_forbidden_outcome_token(application_id)
    assert ":" not in application_id
    filename = application_filename(application_id)
    assert filename == f"credit-application-{application_id}.md"
    assert ":" not in filename
    path = out / filename
    text = path.read_text(encoding="utf-8")
    assert first_visible_line(text) == credit_application_heading(application_id)
    assert not text.lstrip().startswith(WATERMARK)
    assert not text.lstrip().startswith("---")
    assert disclaimer_phrase_in(text) is None
    assert "## Applicant statement" in text
    parsed = parse_application_markdown(text)
    assert parsed["narrative"] in text.split("## Applicant statement", 1)[1]
    assert parsed["application_id"] == application_id
    assert parsed["customer_id"] == "SYN-111111"
    assert parsed["email"].endswith("@example.invalid")
    assert parsed["facility"]["loan_amount"] == "USD 320,000"
    assert parsed["narrative"]
    assert "identity" in text.lower()
    assert "product" in text.lower()
    assert "attached document" in text.lower()
    assert parsed["facility"]["loan_amount"] in text
    for title in parsed["attached_documents"]:
        assert title in text
    assert "application_type" not in parsed
    assert "intended_outcome" not in parsed
    assert "expected_outcome" not in parsed
    assert "expected_judgement" not in text
    assert "expected_outcome" not in text
    assert "application_type" not in text
    assert "intended_outcome" not in text
    assert "missing-data" not in text
    assert (
        validate_record(
            parsed,
            application_type="accepted",
            used=set(),
            required_id=application_id,
            product_family=SLOT_PRODUCT_FAMILY["accepted"],
        )
        == []
    )
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["container"] == "client-applications"
    documents = manifest["documents"]
    assert isinstance(documents, dict)
    row = documents[application_id]
    assert row["intended_outcome"] == "accepted"
    assert row["expected_outcome"] == "accepted"
    assert row["filename"] == filename
    assert row["application_id"] == application_id
    assert "slot" not in row
    assert not (out / "accepted.md").exists()
    extras = [p for p in out.iterdir() if p.suffix not in {".md", ".json"}]
    assert extras == []


def test_generate_all_writes_three_unique_identities_and_products(
    tmp_path: Path,
) -> None:
    out = tmp_path / "apps"
    completer = ScriptedCompleter(
        [
            _valid_record("accepted"),
            _valid_record("rejected"),
            _valid_record("missing-data"),
        ]
    )
    rendered = run_generate_application(
        _config(out, types=APPLICATION_TYPES),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert len(rendered) == 3
    names = {item.record["customer_name"] for item in rendered}
    ids = {item.record["customer_id"] for item in rendered}
    app_ids = {item.record["application_id"] for item in rendered}
    families = {item.record["product_family"] for item in rendered}
    assert len(names) == 3
    assert len(ids) == 3
    assert len(app_ids) == 3
    assert len(families) == 3
    assert families <= set(PRODUCT_FAMILIES)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    documents = manifest["documents"]
    assert isinstance(documents, dict)
    outcomes = {doc["intended_outcome"] for doc in documents.values()}
    assert outcomes == set(APPLICATION_TYPES)
    assert {doc["expected_outcome"] for doc in documents.values()} == set(
        APPLICATION_TYPES
    )
    for application_id, doc in documents.items():
        assert application_id == doc["application_id"]
        assert re.fullmatch(APPLICATION_ID_RE, doc["application_id"])
        assert not contains_forbidden_outcome_token(doc["application_id"])
        assert ":" not in doc["application_id"]
        assert doc["filename"] == f"credit-application-{doc['application_id']}.md"
        assert ":" not in doc["filename"]
        assert (out / doc["filename"]).is_file()
        text = (out / doc["filename"]).read_text(encoding="utf-8")
        assert "intended_outcome" not in text
        assert "expected_outcome" not in text
        assert "application_type" not in text
        assert "missing-data" not in text
        assert "--type" not in text
        assert doc["intended_outcome"] not in text


def test_attached_docs_complete_or_omitted_by_slot(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    completer = ScriptedCompleter(
        [
            _valid_record("accepted"),
            _valid_record("rejected"),
            _valid_record("missing-data"),
        ]
    )
    rendered = run_generate_application(
        _config(out, types=APPLICATION_TYPES),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    by_slot = {item.application_type: item for item in rendered}
    for kind in ("accepted", "rejected"):
        family = by_slot[kind].record["product_family"]
        attached = {
            title.strip() for title in by_slot[kind].record["attached_documents"]
        }
        assert attached == set(PRODUCT_REQUIRED_DOCUMENTS[family])
    missing = by_slot["missing-data"]
    family = missing.record["product_family"]
    attached = {title.strip() for title in missing.record["attached_documents"]}
    required = set(PRODUCT_REQUIRED_DOCUMENTS[family])
    assert attached < required
    assert required - attached


def test_second_generate_appends_distinct_serial(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    first = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_unique_record("accepted", 1)]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    second = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_unique_record("accepted", 2)]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert first[0].record["application_id"] != second[0].record["application_id"]
    assert (out / first[0].filename).is_file()
    assert (out / second[0].filename).is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["documents"]) == {
        first[0].record["application_id"],
        second[0].record["application_id"],
    }


def test_validation_retry_then_success(tmp_path: Path) -> None:
    bad = _valid_record("accepted", email="not-an-invalid-domain@example.com")
    good = _valid_record("accepted")
    completer = ScriptedCompleter([bad, good])
    rendered = run_generate_application(
        _config(tmp_path / "apps"),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert len(rendered) == 1
    assert len(completer.calls) == 2


def test_validation_failure_after_retry_writes_nothing(tmp_path: Path) -> None:
    bad = _valid_record("accepted", email="x@gmail.com")
    out = tmp_path / "apps"
    with pytest.raises(TalosError, match="after retry"):
        run_generate_application(
            _config(out),
            completer=ScriptedCompleter([bad, bad]),
            echo=lambda _: None,
            clock=_frozen_clock,
        )
    assert not out.exists()


def test_missing_project_endpoint_exits_2(
    tmp_path: Path, clean_azure_env: None
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "application",
            "--type",
            "accepted",
            "--local-only",
            "--out",
            str(tmp_path / "apps"),
            "--no-terraform",
        ],
    )
    assert result.exit_code == 2
    assert "GOOGLE_CLOUD_PROJECT" in result.output


def test_force_application_id_keeps_serial(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    first = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_unique_record("accepted", 1)]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    serial = first[0].record["application_id"]
    old_name = first[0].filename
    store = FakeBlobStore(container="client-applications")
    store.blobs[old_name] = FakeBlob(data=b"old", metadata={})
    store.blobs["accepted.md"] = FakeBlob(data=b"legacy", metadata={})
    rendered = run_generate_application(
        _config(
            out,
            local_only=False,
            force=True,
            application_id=serial,
            types=(),
        ),
        completer=ScriptedCompleter(
            [
                _unique_record(
                    "accepted",
                    2,
                    customer_name="New Person",
                    customer_id="SYN-444444",
                )
            ]
        ),
        blob_store=store,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert rendered[0].record["application_id"] == serial
    assert rendered[0].filename == old_name
    assert old_name in store.blobs
    assert "accepted.md" in store.blobs
    text = (out / old_name).read_text(encoding="utf-8")
    assert "New Person" in text
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][serial]["intended_outcome"] == "accepted"
    assert manifest["documents"][serial]["expected_outcome"] == "accepted"
    assert list(manifest["documents"]) == [serial]


def test_old_local_application_files_stay(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    out.mkdir()
    orphan = out / "credit-application-CA-20260115-1768478400999.md"
    orphan.write_text("orphan\n", encoding="utf-8")
    rendered = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert orphan.is_file()
    assert (out / rendered[0].filename).is_file()
    assert rendered[0].filename != orphan.name
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "CA-20260115-1768478400999" not in manifest["documents"]


def test_corrupt_manifest_fails(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    out.mkdir()
    (out / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TalosError, match="invalid application manifest"):
        run_generate_application(
            _config(out),
            completer=ScriptedCompleter([_valid_record("accepted")]),
            echo=lambda _: None,
            clock=_frozen_clock,
        )


def test_optional_blob_upload_hash_metadata(tmp_path: Path) -> None:
    store = FakeBlobStore(container="client-applications")
    completer = ScriptedCompleter([_valid_record("accepted")])
    rendered = run_generate_application(
        _config(tmp_path / "apps", local_only=False),
        completer=completer,
        blob_store=store,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    filename = rendered[0].filename
    assert store.uploads == [filename]
    blob = store.blobs[filename]
    assert blob.metadata["content_sha256"] == rendered[0].content_sha256
    assert blob.metadata["application_id"] == rendered[0].record["application_id"]
    assert "synthetic" not in blob.metadata
    assert "application_type" not in blob.metadata


def test_generate_application_does_not_put_knowledge_source(tmp_path: Path) -> None:
    source = (repo_root() / "src/docgen/application.py").read_text(encoding="utf-8")
    assert "knowledgesources" not in source
    assert "ks-client-applications" not in source


def test_fixtures_parse_and_cover_each_type() -> None:
    fixtures = load_application_fixtures()
    kinds = {item["application_type"] for item in fixtures}
    assert kinds == set(APPLICATION_TYPES)
    families = {item["product_family"] for item in fixtures}
    assert len(families) == 3
    for record in fixtures:
        filename = str(record["filename"])
        path = FIXTURES / filename
        text = path.read_text(encoding="utf-8")
        assert first_visible_line(text) == credit_application_heading(
            str(record["application_id"])
        )
        assert not text.lstrip().startswith(WATERMARK)
        assert not text.lstrip().startswith("---")
        assert disclaimer_phrase_in(text) is None
        assert "## Applicant statement" in text
        heading_end = text.find("\n")
        first_h2 = text.find("\n## ")
        between = text[heading_end:first_h2].strip()
        assert between == ""
        assert filename == f"credit-application-{record['application_id']}.md"
        assert re.fullmatch(APPLICATION_ID_RE, str(record["application_id"]))
        assert not contains_forbidden_outcome_token(str(record["application_id"]))
        assert "intended_outcome" not in text
        assert "expected_outcome" not in text
        assert "application_type" not in text
        assert "expected_judgement" not in text
        parsed = parse_application_markdown(text)
        errors = validate_record(
            parsed,
            application_type=str(record["application_type"]),
            used=set(),
            required_id=str(record["application_id"]),
            product_family=str(record["product_family"]),
        )
        assert errors == [], errors
        family = str(record["product_family"])
        attached = {title.strip() for title in parsed["attached_documents"]}
        required = set(PRODUCT_REQUIRED_DOCUMENTS[family])
        if record["application_type"] == "missing-data":
            assert attached < required
        else:
            assert attached == required


def test_gitignore_covers_generated_applications(repo_root: Path) -> None:
    nested = (repo_root / "data/client-applications/.gitignore").read_text(
        encoding="utf-8"
    )
    assert "*.md" in nested
    assert "manifest.json" in nested


def test_serial_allocator_bumps_unix_ms_on_collision() -> None:
    used = {FROZEN_SERIAL}
    allocator = SerialAllocator(used, clock=_frozen_clock)
    first = allocator.mint()
    assert first == "CA-20260914-1789344000001"
    assert re.fullmatch(APPLICATION_ID_RE, first)
    assert ":" not in first


def test_forbidden_outcome_tokens_are_rejected_in_id() -> None:
    for token in FORBIDDEN_OUTCOME_TOKENS:
        assert contains_forbidden_outcome_token(f"CA-{token}-2026-01")


def test_required_doc_titles_appear_in_published_policies(repo_root: Path) -> None:
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo_root / "data/credit-policies").glob("*.md")
    ).lower()
    for titles in PRODUCT_REQUIRED_DOCUMENTS.values():
        for title in titles:
            assert title.lower() in corpus, title


def test_validate_record_rejects_judgement_tokens_in_narrative() -> None:
    record = _valid_record("accepted", narrative="Please treat this as accepted.")
    errors = validate_record(
        record,
        application_type="accepted",
        used=set(),
        required_id=record["application_id"],
        product_family="residential_mortgage",
    )
    assert any("judgement" in err or "ApplicationType" in err for err in errors)


def test_validate_record_rejects_judgement_tokens_in_email() -> None:
    record = _valid_record("accepted", email="accepted.case@example.invalid")
    errors = validate_record(
        record,
        application_type="accepted",
        used=set(),
        required_id=record["application_id"],
        product_family="residential_mortgage",
    )
    assert any("judgement" in err or "ApplicationType" in err for err in errors)


def test_validate_record_pins_product_label() -> None:
    record = _valid_record("accepted", product="some other mortgage product")
    errors = validate_record(
        record,
        application_type="accepted",
        used=set(),
        required_id=record["application_id"],
        product_family="residential_mortgage",
    )
    assert any("product must be" in err for err in errors)


def test_validate_record_checks_facility_limits() -> None:
    too_high = _valid_record("accepted")
    too_high["facility"] = dict(too_high["facility"], ltv="85%")
    errors = validate_record(
        too_high,
        application_type="accepted",
        used=set(),
        required_id=too_high["application_id"],
        product_family="residential_mortgage",
    )
    assert any("clear published limits" in err for err in errors)
    inside = _valid_record("rejected")
    inside["facility"] = dict(inside["facility"], ltv="60%", dscr="1.40x")
    errors = validate_record(
        inside,
        application_type="rejected",
        used=set(),
        required_id=inside["application_id"],
        product_family="commercial_real_estate",
    )
    assert any("breach at least one" in err for err in errors)


def test_validate_record_requires_policy_facts_on_complete_files() -> None:
    incomplete = _valid_record("accepted")
    incomplete["facility"] = {
        key: value
        for key, value in incomplete["facility"].items()
        if key not in {"appraisal_date", "licensed_appraiser"}
    }
    errors = validate_record(
        incomplete,
        application_type="accepted",
        used=set(),
        required_id=incomplete["application_id"],
        product_family="residential_mortgage",
    )
    assert any("appraisal_date" in err for err in errors)


def test_seed_application_fixtures_copies_gold_trio(tmp_path: Path) -> None:
    dest = tmp_path / "apps"
    copied = seed_application_fixtures(dest, FIXTURES)
    assert copied == 3
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    documents = manifest["documents"]
    assert documents["CA-20260115-1768478400000"]["expected_outcome"] == "accepted"
    assert documents["CA-20260220-1771588800000"]["expected_outcome"] == "rejected"
    assert documents["CA-20260325-1774440000000"]["expected_outcome"] == "missing-data"
    assert (dest / "credit-application-CA-20260115-1768478400000.md").is_file()
    again = seed_application_fixtures(dest, FIXTURES)
    assert again == 0


def test_parse_llm_json_strips_fence() -> None:
    data = parse_llm_json('```json\n{"a": 1}\n```')
    assert data == {"a": 1}


def test_generated_markdown_opens_with_heading_not_watermark(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    rendered = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    text = (out / rendered[0].filename).read_text(encoding="utf-8")
    heading = credit_application_heading(rendered[0].record["application_id"])
    assert first_visible_line(text) == heading
    assert text.startswith(heading)
    assert WATERMARK not in text
    assert disclaimer_phrase_in(text) is None


def test_generated_markdown_has_no_yaml_frontmatter(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    rendered = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    text = (out / rendered[0].filename).read_text(encoding="utf-8")
    before_heading: list[str] = []
    for line in text.splitlines():
        if line.startswith("# Credit application "):
            break
        before_heading.append(line.strip())
    assert "---" not in before_heading
    with pytest.raises(TalosError, match="YAML front matter"):
        parse_application_markdown(
            f"---\napplication_id: {SAMPLE_SERIAL}\n---\n"
            f"# Credit application {SAMPLE_SERIAL}\n"
        )


def test_parse_rejects_watermark_prefix() -> None:
    with pytest.raises(TalosError, match="must not start with the policy watermark"):
        parse_application_markdown(
            f"{WATERMARK}\n\n# Credit application {SAMPLE_SERIAL}\n"
        )


def test_parse_rejects_watermark_anywhere() -> None:
    with pytest.raises(TalosError, match="must not contain"):
        parse_application_markdown(
            f"# Credit application {SAMPLE_SERIAL}\n\n"
            "## Identity\n\n- Name: Pat\n\n"
            f"## Applicant statement\n\n{WATERMARK} I apply.\n"
        )


@pytest.mark.parametrize("phrase", APPLICATION_DISCLAIMER_PHRASES)
def test_validate_record_rejects_disclaimer_phrases(phrase: str) -> None:
    record = _valid_record("accepted", narrative=f"Please review this file. {phrase}")
    errors = validate_record(
        record,
        application_type="accepted",
        used=set(),
        required_id=record["application_id"],
        product_family="residential_mortgage",
    )
    assert any("must not contain" in err for err in errors), errors


def test_validate_filing_markdown_rejects_preamble() -> None:
    record = _valid_record("accepted")
    heading = credit_application_heading(str(record["application_id"]))
    markdown = (
        f"{heading}\n\n"
        "I am filing this synthetic credit application with Contoso Demo Bank. "
        "This is not a real borrower record.\n\n"
        "## Identity\n\n- Name: Pat Rivet\n\n"
        "## Product\n\nowner-occupied residential mortgage\n\n"
        "## Amount and financials\n\n- loan_amount: USD 320,000\n\n"
        "## Attached documents\n\n- last 2 pay stubs\n- W-2\n- residential appraisal\n\n"
        f"## Applicant statement\n\n{record['narrative']}\n"
    )
    errors = validate_filing_markdown(markdown, record)
    assert any("preamble" in err for err in errors)
    assert any("must not contain" in err for err in errors)


def test_generated_markdown_has_no_preamble_and_statement_holds_narrative(
    tmp_path: Path,
) -> None:
    out = tmp_path / "apps"
    rendered = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    text = (out / rendered[0].filename).read_text(encoding="utf-8")
    heading_end = text.find("\n")
    first_h2 = text.find("\n## ")
    assert text[heading_end:first_h2].strip() == ""
    assert "## Identity" in text
    assert text.index("## Applicant statement") > text.index("## Identity")
    narrative = str(rendered[0].record["narrative"])
    statement = text.split("## Applicant statement", 1)[1].strip()
    assert statement == narrative
    assert disclaimer_phrase_in(text) is None


def test_generate_prompts_do_not_inject_disclaimer_phrases(tmp_path: Path) -> None:
    system = (repo_root() / "corpus/application/system.md").read_text(encoding="utf-8")
    user_src = (repo_root() / "corpus/application/user.md.j2").read_text(
        encoding="utf-8"
    )
    template = (repo_root() / "corpus/application/document.md.j2").read_text(
        encoding="utf-8"
    )
    for phrase in APPLICATION_DISCLAIMER_PHRASES:
        assert phrase not in system
        assert phrase not in user_src
        assert phrase not in template
    assert "{{ watermark }}" not in user_src
    facts = FACTS.read_text(encoding="utf-8")
    assert WATERMARK not in facts
    assert WATERMARK not in strip_watermark_from_facts_yaml(facts)
    completer = ScriptedCompleter([_valid_record("accepted")])
    run_generate_application(
        _config(tmp_path / "apps"),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    blob = json.dumps(completer.calls, ensure_ascii=False)
    for phrase in APPLICATION_DISCLAIMER_PHRASES:
        assert phrase not in blob
    assert WATERMARK not in blob


def test_force_without_application_id_is_usage_error() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "application",
            "--type",
            "accepted",
            "--force",
            "--local-only",
            "--no-terraform",
        ],
    )
    assert result.exit_code == 1
    assert "--application-id" in result.output


def test_force_with_count_is_usage_error() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            "application",
            "--type",
            "accepted",
            "--force",
            "--application-id",
            FROZEN_SERIAL,
            "--count",
            "2",
            "--local-only",
            "--no-terraform",
        ],
    )
    assert result.exit_code == 1
    assert "--count" in result.output


def test_count_without_type_or_all_is_usage_error() -> None:
    result = CliRunner().invoke(
        cli,
        ["generate", "application", "--count", "2", "--local-only", "--no-terraform"],
    )
    assert result.exit_code == 1
    assert "--count" in result.output
    assert "--type" in result.output or "--all" in result.output


class _AdvancingClock:
    def __init__(self, start: datetime, step_ms: int = 5) -> None:
        self._current = start
        self._step_ms = step_ms

    def __call__(self) -> datetime:
        now = self._current
        self._current = datetime.fromtimestamp(
            now.timestamp() + self._step_ms / 1000.0,
            tz=timezone.utc,
        )
        return now


class _EchoIdCompleter:
    def complete(self, *, messages: list[dict[str, str]]) -> str:
        blob = "\n".join(message["content"] for message in messages)
        match = re.search(r"application_id:\s*(CA-\d{8}-\d+)", blob)
        assert match is not None
        serial = match.group(1)
        record: dict[str, Any] = _valid_record(
            "accepted",
            application_id=serial,
            narrative=(
                f"I request this synthetic demo facility under {serial} "
                "and list the documents I am submitting."
            ),
        )
        return json.dumps(record)


def test_peek_and_mint_keep_one_serial_when_clock_advances(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    rendered = run_generate_application(
        _config(out),
        completer=_EchoIdCompleter(),
        echo=lambda _: None,
        clock=_AdvancingClock(FROZEN_NOW),
    )
    serial = rendered[0].record["application_id"]
    text = (out / rendered[0].filename).read_text(encoding="utf-8")
    assert serial == FROZEN_SERIAL
    assert rendered[0].filename == application_filename(serial)
    assert credit_application_heading(serial) in text
    assert serial in text
    parsed = parse_application_markdown(text)
    assert parsed["application_id"] == serial
    assert serial in parsed["narrative"]


def test_serial_allocator_issues_100_distinct_ids_when_clock_frozen() -> None:
    allocator = SerialAllocator(clock=_frozen_clock)
    serials = [allocator.mint() for _ in range(100)]
    assert len(set(serials)) == 100
    assert serials[0] == FROZEN_SERIAL
    assert serials[1] == "CA-20260914-1789344000001"
    assert serials[-1] == "CA-20260914-1789344000099"
    for serial in serials:
        assert re.fullmatch(APPLICATION_ID_RE, serial)
        assert ":" not in serial
        assert not contains_forbidden_outcome_token(serial)


def test_count_100_writes_100_distinct_files_and_manifest_rows(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    completer = ScriptedCompleter(
        [_unique_record("accepted", index) for index in range(1, 101)]
    )
    rendered = run_generate_application(
        _config(out, count=100),
        completer=completer,
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert len(rendered) == 100
    ids = [item.record["application_id"] for item in rendered]
    assert len(set(ids)) == 100
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["documents"]) == set(ids)
    files = list(out.glob("credit-application-*.md"))
    assert len(files) == 100
    for item in rendered:
        assert (out / item.filename).is_file()
        assert manifest["documents"][item.record["application_id"]]["filename"] == (
            item.filename
        )
        assert ":" not in item.filename


def test_all_count_100_writes_300_distinct_files(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    payloads = (
        [_unique_record("accepted", index) for index in range(1, 101)]
        + [_unique_record("rejected", index) for index in range(101, 201)]
        + [_unique_record("missing-data", index) for index in range(201, 301)]
    )
    rendered = run_generate_application(
        _config(out, types=APPLICATION_TYPES, count=100),
        completer=ScriptedCompleter(payloads),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert len(rendered) == 300
    ids = [item.record["application_id"] for item in rendered]
    assert len(set(ids)) == 300
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 300
    by_outcome: dict[str, int] = {"accepted": 0, "rejected": 0, "missing-data": 0}
    for doc in manifest["documents"].values():
        by_outcome[doc["intended_outcome"]] += 1
    assert by_outcome == {"accepted": 100, "rejected": 100, "missing-data": 100}
    assert len(list(out.glob("credit-application-*.md"))) == 300


def test_dry_run_count_prints_serials_without_writing(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    lines: list[str] = []
    rendered = run_generate_application(
        _config(out, count=3, dry_run=True),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lines.append,
        clock=_frozen_clock,
    )
    assert rendered == []
    assert not out.exists()
    serials = [line for line in lines if "credit-application-CA-" in line]
    assert len(serials) == 3
    assert FROZEN_SERIAL in serials[0]
    assert "1789344000001" in serials[1]


def test_count_fail_keeps_prior_serials(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    bad = _unique_record("accepted", 3, email="x@gmail.com")
    with pytest.raises(TalosError, match="after retry"):
        run_generate_application(
            _config(out, count=3),
            completer=ScriptedCompleter(
                [_unique_record("accepted", 1), _unique_record("accepted", 2), bad, bad]
            ),
            echo=lambda _: None,
            clock=_frozen_clock,
        )
    files = list(out.glob("credit-application-*.md"))
    assert len(files) == 2
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 2


def test_llm_fail_does_not_consume_serial(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    bad = _valid_record("accepted", email="x@gmail.com")
    with pytest.raises(TalosError, match="after retry"):
        run_generate_application(
            _config(out),
            completer=ScriptedCompleter([bad, bad]),
            echo=lambda _: None,
            clock=_frozen_clock,
        )
    assert not out.exists()
    rendered = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_valid_record("accepted")]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    assert rendered[0].record["application_id"] == FROZEN_SERIAL


def test_force_type_updates_intended_outcome(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    first = run_generate_application(
        _config(out),
        completer=ScriptedCompleter([_unique_record("accepted", 1)]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    serial = first[0].record["application_id"]
    run_generate_application(
        _config(out, force=True, application_id=serial, types=("rejected",)),
        completer=ScriptedCompleter([_unique_record("rejected", 2)]),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][serial]["intended_outcome"] == "rejected"
    assert manifest["documents"][serial]["expected_outcome"] == "rejected"
    assert list(manifest["documents"]) == [serial]


def test_application_id_does_not_encode_type(tmp_path: Path) -> None:
    out = tmp_path / "apps"
    rendered = run_generate_application(
        _config(out, types=APPLICATION_TYPES),
        completer=ScriptedCompleter(
            [
                _unique_record("accepted", 1),
                _unique_record("rejected", 2),
                _unique_record("missing-data", 3),
            ]
        ),
        echo=lambda _: None,
        clock=_frozen_clock,
    )
    for item in rendered:
        serial = item.record["application_id"]
        assert re.fullmatch(APPLICATION_ID_RE, serial)
        upper = serial.upper()
        for token in ("ACCEPTED", "REJECTED", "MISSING"):
            assert token not in upper
        assert item.application_type not in serial
