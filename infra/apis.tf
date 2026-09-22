resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project
  service            = each.value
  disable_on_destroy = false
}
