output "GOOGLE_CLOUD_PROJECT" {
  value = var.project
}

output "GOOGLE_CLOUD_LOCATION" {
  value = var.region
}

output "GCS_BUCKET" {
  value = google_storage_bucket.documents.name
}

output "GCS_URI" {
  value = "gs://${google_storage_bucket.documents.name}"
}

output "GOOGLE_AGENT_SERVICE_ACCOUNT" {
  value = google_service_account.agent.email
}
