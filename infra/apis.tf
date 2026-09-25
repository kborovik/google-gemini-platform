resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "discoveryengine.googleapis.com",
    "dns.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "siteverification.googleapis.com",
    "storage.googleapis.com",
    "telemetry.googleapis.com",
  ])

  project            = var.project
  service            = each.value
  disable_on_destroy = false
}
