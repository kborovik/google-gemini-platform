from __future__ import annotations

import re

import pytest

from docgen.env import repo_root

pytestmark = pytest.mark.unit


def test_reasoning_engine_service_agent_can_impersonate_the_agent() -> None:
    text = (repo_root() / "infra/iam.tf").read_text(encoding="utf-8")
    assert "gcp-sa-aiplatform-re.iam.gserviceaccount.com" in text
    assert 'role               = "roles/iam.serviceAccountUser"' in text
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in text


def test_credit_officer_can_write_cloud_trace() -> None:
    apis = (repo_root() / "infra/apis.tf").read_text(encoding="utf-8")
    iam = (repo_root() / "infra/iam.tf").read_text(encoding="utf-8")
    agent = (repo_root() / "infra/agent.tf").read_text(encoding="utf-8")
    requirements = (repo_root() / "agents/credit_officer/requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "telemetry.googleapis.com" in apis
    assert "logging.googleapis.com" in apis
    assert 'role    = "roles/telemetry.tracesWriter"' in iam
    assert 'role    = "roles/logging.logWriter"' in iam
    assert "google_project_iam_member.agent_traces" in agent
    assert "google_project_iam_member.agent_logs" in agent
    assert re.search(
        r'name\s+=\s+"GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"\s+'
        r'value\s+=\s+"true"',
        agent,
    )
    # Distribution provides opentelemetry.exporter.cloud_logging.
    assert "opentelemetry-exporter-gcp-logging>=1.9.0a0,<=1.12.0a0" in requirements


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
    assert 'resource "google_discovery_engine_data_store" "kb_credit_policies"' in text
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert "infra-rag-destroy" not in makefile
    assert "$(UV) run docgen index" in makefile
    assert "python -m docgen.search_index" not in makefile


def test_terraform_creates_empty_agent_search_data_store() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo_root() / "infra").glob("*.tf"))
    )
    match = re.search(
        r'resource "google_discovery_engine_data_store" "kb_credit_policies" \{'
        r"(?P<body>.*?)\n\}",
        text,
        re.S,
    )
    assert match is not None
    body = match.group("body")
    assert re.search(r'location\s+=\s+"global"', body)
    assert re.search(r'data_store_id\s+=\s+"kb-credit-policies"', body)
    assert re.search(r'display_name\s+=\s+"kb-credit-policies"', body)
    assert re.search(r'industry_vertical\s+=\s+"GENERIC"', body)
    assert re.search(r'content_config\s+=\s+"CONTENT_REQUIRED"', body)
    assert re.search(r'solution_types\s+=\s+\["SOLUTION_TYPE_SEARCH"\]', body)
    assert re.search(r"project\s+=\s+var\.project", body)
    assert "document_processing_config" in body
    assert "digital_parsing_config" in body
    assert "us-east1" not in body
    assert "us-east5" not in body
    assert "import" not in body
    assert "google_discovery_engine_document" not in text
    assert re.search(
        r'output "DATA_STORE" \{\s*'
        r"value = google_discovery_engine_data_store\.kb_credit_policies\.name",
        text,
    )


def test_provider_bills_discovery_engine_to_the_workload_project() -> None:
    text = (repo_root() / "infra/versions.tf").read_text(encoding="utf-8")
    assert "billing_project       = var.project" in text
    assert "user_project_override = true" in text


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


_ADK_CLASS_METHODS = {
    "get_session": "",
    "async_get_session": "async",
    "list_sessions": "",
    "async_list_sessions": "async",
    "create_session": "",
    "async_create_session": "async",
    "delete_session": "",
    "async_delete_session": "async",
    "stream_query": "stream",
    "async_stream_query": "async_stream",
    "streaming_agent_run_with_events": "async_stream",
}


def test_makefile_deploy_applies_uploads_and_indexes() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^deploy:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert match is not None
    body = match.group(1)
    assert "terraform-apply" in match.group(0)
    assert "output -raw DATA_STORE" in body
    assert "agents/credit_officer/.env" in body
    assert "$(UV) run docgen upload" in body
    assert "$(MAKE) index wait=1" in body
    assert "adk deploy" not in text
    generated = re.search(r"^generate:[^\n]*\n((?:[ \t].*\n)*)", text, re.M)
    assert generated is not None
    assert "docgen generate application --all --local-only" in generated.group(1)
    assert "docgen generate" not in body


def test_terraform_deploys_credit_officer_reasoning_engine() -> None:
    infra = repo_root() / "infra"
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(infra.glob("*.tf"))
    )
    assert 'resource "google_vertex_ai_reasoning_engine" "credit_officer"' in text
    assert "package_spec" not in text
    match = re.search(
        r'resource "google_vertex_ai_reasoning_engine" "credit_officer" \{'
        r"(?P<body>.*?)\n\}",
        text,
        re.S,
    )
    assert match is not None
    body = match.group("body")
    assert re.search(r"project\s+=\s+var\.project", body)
    assert re.search(r"region\s+=\s+var\.region", body)
    assert re.search(r'display_name\s+=\s+"credit-officer"', body)
    assert re.search(r'agent_framework\s+=\s+"google-adk"', body)
    assert "source_code_spec" in body
    assert "inline_source" in body
    assert "filebase64(data.archive_file.credit_officer.output_path)" in body
    assert 'output_file_mode = "0644"' in text
    assert re.search(r'entrypoint_module\s+=\s+"agent"', body)
    assert re.search(r'entrypoint_object\s+=\s+"root_agent"', body)
    assert re.search(r'requirements_file\s+=\s+"requirements.txt"', body)
    assert re.search(r'version\s+=\s+"3.14"', body)
    assert "class_methods" in body
    assert "jsonencode(local.adk_class_methods)" in body
    assert re.search(
        r"service_account\s+=\s+google_service_account\.agent\.email",
        body,
    )
    assert re.search(
        r'name\s+=\s+"DATA_STORE"\s+'
        r"value\s+=\s+google_discovery_engine_data_store\.kb_credit_policies\.name",
        body,
    )
    archive = re.search(
        r'data "archive_file" "credit_officer" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert archive is not None
    filenames = re.findall(r'filename\s+=\s+"([^"]+)"', archive.group("body"))
    assert filenames == [
        "agent.py",
        "credit-policy-agent.instructions.md",
        "requirements.txt",
    ]
    requirements = (repo_root() / "agents/credit_officer/requirements.txt").read_text(
        encoding="utf-8"
    )
    assert [line.strip() for line in requirements.splitlines() if line.strip()] == [
        "google-adk>=2.9.2,<3",
        "google-cloud-aiplatform[agent_engines]>=1.128.0,<2",
        "opentelemetry-exporter-gcp-logging>=1.9.0a0,<=1.12.0a0",
    ]
    methods = re.findall(
        r'\bname\s+=\s+"([^"]+)"\s+api_mode\s+=\s+"([^"]*)"',
        text,
    )
    assert dict(methods) == _ADK_CLASS_METHODS
    assert len(methods) == 11
    assert 'account_id   = "credit-policy-agent"' in text
    assert re.search(
        r'output "REASONING_ENGINE" \{\s*'
        r"value = google_vertex_ai_reasoning_engine\.credit_officer\.name",
        text,
    )


def test_makefile_has_no_chat_recipe() -> None:
    text = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^chat:", text, re.M) is None
    assert re.search(r"^chat-deploy:", text, re.M) is None
    assert "CHAT_PORT" not in text
    assert "cloudflared" not in text
    assert "need-cloudflared" not in text


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
    assert "gcloud auth login --no-launch-browser" in text
    assert "gcloud auth login --update-adc" not in text
    assert (
        "gcloud auth application-default login --no-launch-browser "
        '--scopes="openid,https://www.googleapis.com/auth/userinfo.email,'
        "https://www.googleapis.com/auth/cloud-platform,"
        "https://www.googleapis.com/auth/sqlservice.login,"
        'https://www.googleapis.com/auth/siteverification"'
    ) in text
    assert "gcloud auth application-default set-quota-project $(google_project)" in text
    assert "gcloud config set core/project $(google_project)" in text
    assert "gcloud config set compute/region $(google_region)" in text
    assert "gcloud config set compute/zone $(google_zone)" in text
    assert "gcloud auth revoke --all" in text


def _infra_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo_root() / "infra").glob("*.tf"))
    )


def test_v8_cloud_run_chat_and_registry_use_workload_region() -> None:
    text = _infra_text()
    assert "run.googleapis.com" in text
    assert "artifactregistry.googleapis.com" in text
    assert "chat.googleapis.com" not in text
    assert "us-east5" not in text
    for kind in (
        'resource "google_cloud_run_v2_service" "chat"',
        'resource "google_artifact_registry_repository" "chat"',
    ):
        match = re.search(re.escape(kind) + r" \{(?P<body>.*?)\n\}", text, re.S)
        assert match is not None, kind
        body = match.group("body")
        assert re.search(r"location\s+=\s+var\.region", body), kind
        assert re.search(r"project\s+=\s+var\.project", body), kind


def test_v16_chat_invoker_is_a_literal() -> None:
    names = re.findall(
        r'^variable "([^"]+)"',
        (repo_root() / "infra/variables.tf").read_text(encoding="utf-8"),
        re.M,
    )
    assert names == ["project", "region"]
    iam = (repo_root() / "infra/iam.tf").read_text(encoding="utf-8")
    assert 'role     = "roles/run.invoker"' in iam
    assert 'member   = "serviceAccount:chat@system.gserviceaccount.com"' in iam
    assert "allUsers" not in _infra_text()


def test_v18_chat_handler_host() -> None:
    text = _infra_text()
    service = re.search(
        r'resource "google_cloud_run_v2_service" "chat" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert service is not None
    body = service.group("body")
    assert 'name                = "chat"' in body or re.search(
        r'name\s+=\s+"chat"', body
    )
    assert 'ingress             = "INGRESS_TRAFFIC_ALL"' in body or re.search(
        r'ingress\s+=\s+"INGRESS_TRAFFIC_ALL"', body
    )
    assert re.search(
        r'custom_audiences\s+=\s+\["https://credit-policy\.ai\.lab5\.ca"\]',
        body,
    )
    assert re.search(r'timeout\s+=\s+"300s"', body)
    assert re.search(r"min_instance_count\s+=\s+1", body)
    assert re.search(
        r"service_account\s+=\s+google_service_account\.agent\.email", body
    )
    assert re.search(r"image\s+=\s+local\.chat_image", body)
    assert 'command = ["python", "main.py"]' in body
    assert "cloudbuild.googleapis.com" in text
    assert 'account_id   = "chat-builder"' in text
    assert "GCS_ONLY" in (repo_root() / "chat/cloudbuild.yaml").read_text(
        encoding="utf-8"
    )
    assert (
        "${var.region}-docker.pkg.dev/${var.project}/chat/handler:${data.archive_file.chat.output_md5}"
        in text
    )
    assert "gcloud builds submit" in text
    assert "terraform_data.chat_image" in body
    mapping = re.search(
        r'resource "google_cloud_run_domain_mapping" "chat" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert mapping is not None
    mapped = mapping.group("body")
    assert re.search(r'name\s+=\s+"credit-policy\.ai\.lab5\.ca"', mapped)
    assert re.search(r"location\s+=\s+var\.region", mapped)
    assert re.search(
        r"route_name\s+=\s+google_cloud_run_v2_service\.chat\.name", mapped
    )
    assert re.search(
        r'output "CHAT_HOST" \{\s*value = "credit-policy\.ai\.lab5\.ca"', text
    )
    zone = re.search(
        r'resource "google_dns_managed_zone" "ai" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert zone is not None
    zone_body = zone.group("body")
    assert re.search(r'name\s+=\s+"ai-lab5-ca"', zone_body)
    assert re.search(r'dns_name\s+=\s+"ai\.lab5\.ca\."', zone_body)
    assert re.search(r'visibility\s+=\s+"public"', zone_body)
    assert re.search(r"project\s+=\s+var\.project", zone_body)
    assert re.search(r'dns_name\s+=\s+"lab5\.ca\."', text) is None
    record = re.search(
        r'resource "google_dns_record_set" "credit_policy" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert record is not None
    record_body = record.group("body")
    assert re.search(r'name\s+=\s+"credit-policy\.ai\.lab5\.ca\."', record_body)
    assert re.search(r'type\s+=\s+"CNAME"', record_body)
    assert re.search(r'rrdatas\s+=\s+\["ghs\.googlehosted\.com\."\]', record_body)
    assert re.search(
        r"managed_zone\s+=\s+google_dns_managed_zone\.ai\.name", record_body
    )
    token = re.search(
        r'data "google_site_verification_token" "ai" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert token is not None
    token_body = token.group("body")
    assert re.search(r'type\s+=\s+"INET_DOMAIN"', token_body)
    assert re.search(r'identifier\s+=\s+"ai\.lab5\.ca"', token_body)
    assert re.search(r'verification_method\s+=\s+"DNS_TXT"', token_body)
    assert "google_project_service.apis" in token_body
    txt = re.search(
        r'resource "google_dns_record_set" "ai_verification" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert txt is not None
    txt_body = txt.group("body")
    assert re.search(r'name\s+=\s+"ai\.lab5\.ca\."', txt_body)
    assert re.search(r'type\s+=\s+"TXT"', txt_body)
    assert re.search(
        r"rrdatas\s+=\s+\[data\.google_site_verification_token\.ai\.token\]",
        txt_body,
    )
    assert re.search(r"managed_zone\s+=\s+google_dns_managed_zone\.ai\.name", txt_body)
    web = re.search(
        r'resource "google_site_verification_web_resource" "ai" \{(?P<body>.*?)\n\}',
        text,
        re.S,
    )
    assert web is not None
    web_body = web.group("body")
    assert re.search(r'type\s+=\s+"INET_DOMAIN"', web_body)
    assert re.search(r'identifier\s+=\s+"ai\.lab5\.ca"', web_body)
    assert re.search(r'verification_method\s+=\s+"DNS_TXT"', web_body)
    assert re.search(r'deletion_policy\s+=\s+"ABANDON"', web_body)
    assert "google_dns_record_set.ai_verification" in web_body
    assert 'identifier          = "lab5.ca"' not in text
    assert "google_site_verification_web_resource.ai" in mapped
    assert len(re.findall(r'resource "google_dns_record_set"', text)) == 2
    assert "dns.googleapis.com" in text
    assert "siteverification.googleapis.com" in text
    assert re.search(
        r'output "AI_ZONE_NS" \{\s*'
        r"value = google_dns_managed_zone\.ai\.name_servers",
        text,
    )
    for banned in (
        "google_compute_global_address",
        "google_compute_global_forwarding_rule",
        "google_compute_url_map",
        "google_compute_target_https_proxy",
        "kronos",
        'provider "cloudflare"',
        "cloudflare/cloudflare",
        "terraform-cloudflare-modules",
        'module "chat_dns"',
        'resource "cloudflare_zone"',
        "allUsers",
    ):
        assert banned not in text, banned
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^chat:", makefile, re.M) is None
    assert re.search(r"^chat-deploy:", makefile, re.M) is None
    assert "docker build" not in makefile
    assert "docker push" not in makefile
    assert "need-docker" not in makefile
    assert "adk deploy" not in makefile
