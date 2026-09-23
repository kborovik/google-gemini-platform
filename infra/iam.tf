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
