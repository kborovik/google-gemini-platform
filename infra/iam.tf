resource "google_service_account" "agent" {
  account_id   = "credit-policy-agent"
  display_name = "Credit Policy Agent"
  project      = var.project

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "agent_viewer" {
  bucket = google_storage_bucket.documents.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.agent.member
}

resource "google_project_iam_member" "agent_aiplatform" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = google_service_account.agent.member

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "agent_discoveryengine" {
  project = var.project
  role    = "roles/discoveryengine.viewer"
  member  = google_service_account.agent.member

  depends_on = [google_project_service.apis]
}

# AdkApp enable_tracing exports OTLP spans to telemetry.googleapis.com.
resource "google_project_iam_member" "agent_traces" {
  project = var.project
  role    = "roles/telemetry.tracesWriter"
  member  = google_service_account.agent.member

  depends_on = [google_project_service.apis]
}

# Cloud Logging export writes through logging.googleapis.com.
resource "google_project_iam_member" "agent_logs" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = google_service_account.agent.member

  depends_on = [google_project_service.apis]
}

# Agent Runtime starts the revision as this account. Without these bindings the
# Reasoning Engine service agent cannot mint its token, and the revision never serves.
resource "google_service_account_iam_member" "reasoning_engine_service_agent_user" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

resource "google_service_account_iam_member" "reasoning_engine_service_agent_token_creator" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# Content import creates a staging bucket in this project. The Google-managed
# service agent is not bound automatically here, so documents:import 403s.
data "google_project" "current" {
  project_id = var.project
}

resource "google_project_iam_member" "discoveryengine_service_agent" {
  project = var.project
  role    = "roles/discoveryengine.serviceAgent"
  member  = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

# Google Chat calls the handler as this service account.
resource "google_cloud_run_v2_service_iam_member" "chat_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.chat.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:chat@system.gserviceaccount.com"
}
