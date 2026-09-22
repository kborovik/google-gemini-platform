resource "google_storage_bucket" "documents" {
  name                        = "${var.project}-credit-docs"
  location                    = var.region
  project                     = var.project
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  depends_on = [google_project_service.apis]
}
