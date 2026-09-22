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
}
