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

output "DATA_STORE" {
  value = google_discovery_engine_data_store.kb_credit_policies.name
}

output "REASONING_ENGINE" {
  value = google_vertex_ai_reasoning_engine.credit_officer.name
}
