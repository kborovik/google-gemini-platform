# Project-level RAG Engine tier. The corpus itself is created by `talos deploy`
# (same split as Foundry: Terraform owns the platform, the CLI owns the index).
resource "google_vertex_ai_rag_engine_config" "basic" {
  region  = var.region
  project = var.project

  rag_managed_db_config {
    basic {}
  }

  depends_on = [google_project_service.apis]
}
