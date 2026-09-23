resource "google_discovery_engine_data_store" "kb_credit_policies" {
  project           = var.project
  location          = "global"
  data_store_id     = "kb-credit-policies"
  display_name      = "kb-credit-policies"
  industry_vertical = "GENERIC"
  content_config    = "CONTENT_REQUIRED"
  solution_types    = ["SOLUTION_TYPE_SEARCH"]

  depends_on = [google_project_service.apis]
}
