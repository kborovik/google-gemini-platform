resource "google_discovery_engine_data_store" "kb_credit_policies" {
  project           = var.project
  location          = "global"
  data_store_id     = "kb-credit-policies"
  display_name      = "kb-credit-policies"
  industry_vertical = "GENERIC"
  content_config    = "CONTENT_REQUIRED"
  solution_types    = ["SOLUTION_TYPE_SEARCH"]

  # The API default. Omitting it makes Terraform replace a store that already has it.
  document_processing_config {
    default_parsing_config {
      digital_parsing_config {}
    }
  }

  depends_on = [google_project_service.apis]
}
