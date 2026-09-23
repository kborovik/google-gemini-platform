from __future__ import annotations

import re

import pytest

from docgen.env import repo_root

pytestmark = pytest.mark.unit


def test_discoveryengine_service_agent_can_stage_imports() -> None:
    text = (repo_root() / "infra/iam.tf").read_text(encoding="utf-8")
    assert 'role    = "roles/discoveryengine.serviceAgent"' in text
    assert "gcp-sa-discoveryengine.iam.gserviceaccount.com" in text


def test_terraform_has_no_rag_engine_tier() -> None:
    infra = repo_root() / "infra"
    assert not (infra / "rag.tf").exists()
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(infra.glob("*.tf"))
    )
    assert "google_vertex_ai_rag_engine_config" not in text
    assert "us-east5" not in text
    assert "discoveryengine.googleapis.com" in text
    assert "aiplatform.googleapis.com" in text
    assert "storage.googleapis.com" in text
    assert (
        "locations/global/collections/default_collection/dataStores/kb-credit-policies"
        in text
    )
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "infra-rag-destroy" not in makefile
    assert "$(UV) run docgen index" in makefile
    assert "python -m docgen.search_index" not in makefile


def test_terraform_variables_are_only_project_and_region() -> None:
    text = (repo_root() / "infra/variables.tf").read_text(encoding="utf-8")
    names = re.findall(r'^variable "([^"]+)"', text, re.M)
    assert names == ["project", "region"]
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "TF_VAR_FILE := $(PROJECT).tfvars" in makefile
    assert "-var-file=$(TF_VAR_FILE)" in makefile
    assert "PROJECT ?= lab5-gemini-dev1" in makefile


def test_tfvars_pin_lab5_gemini_dev1() -> None:
    text = (repo_root() / "infra/lab5-gemini-dev1.tfvars").read_text(encoding="utf-8")
    assert 'project = "lab5-gemini-dev1"' in text
    assert 'region  = "us-east1"' in text


def test_makefile_deploy_applies_uploads_indexes_and_deploys_adk() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^deploy:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert match is not None
    body = match.group(1)
    assert "terraform-apply" in match.group(0)
    assert "output -raw DATA_STORE" in body
    assert "agents/credit_officer/.env" in body
    assert "$(UV) run docgen upload" in body
    assert "$(MAKE) index wait=1" in body
    assert "adk deploy agent_engine" in body
    assert "--project=$(PROJECT)" in body
    assert "--region=$(REGION)" in body
    assert "--display_name=credit-officer" in body
    assert "agents/credit_officer" in body
    generated = re.search(r"^generate:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert generated is not None
    assert "docgen generate application --all --local-only" in generated.group(1)
    assert "docgen generate" not in body


def test_makefile_index_wait_flag() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^index:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert match is not None
    body = match.group(1)
    assert "$(UV) run docgen index $(if $(wait),--wait,)" in body
    assert "us-east5" not in text
    assert "google_vertex_ai_rag_engine_config" not in text


def test_makefile_uses_org_factory_state_bucket() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "PROJECT ?= lab5-gemini-dev1" in text
    assert "TFSTATE_BUCKET := terraform-$(PROJECT)" in text
    assert "infra-backend-create" not in text
    assert "infra-backend-destroy" not in text
    assert "buckets create" not in text
    assert "gcloud projects create" not in text


def test_makefile_terraform_recipes_follow_org_factory() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "terraform -chdir=$(terraform_dir) fmt -check -recursive" in text
    assert "init -input=false -upgrade -reconfigure" in text
    assert '-backend-config="bucket=$(terraform_bucket)"' in text
    assert '-backend-config="prefix=$(TF_PREFIX)"' in text
    assert "plan -input=false -refresh=true -var-file=$(TF_VAR_FILE)" in text
    assert "apply -auto-approve -input=false -var-file=$(TF_VAR_FILE)" in text
    assert "output -json > $(terraform_dir)/outputs.json" in text
    assert "apply -destroy -input=false -refresh=true -var-file=$(TF_VAR_FILE)" in text
    assert "rm -f $(terraform_dir)/outputs.json" in text
    assert re.search(r"^infra-", text, re.M) is None
    assert "gcloud auth login --update-adc --no-launch-browser" in text
    assert "gcloud auth application-default set-quota-project $(google_project)" in text
    assert "gcloud config set core/project $(google_project)" in text
    assert "gcloud config set compute/region $(google_region)" in text
    assert "gcloud config set compute/zone $(google_zone)" in text
    assert "gcloud auth revoke --all" in text
