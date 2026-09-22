# New projects are not allowlisted for RAG in us-central1, us-east1, or
# us-east4. The document bucket stays in var.region. talos deploy creates
# the corpus in us-east5, and this is the Spanner tier for that region.
resource "google_vertex_ai_rag_engine_config" "basic" {
  region  = "us-east5"
  project = var.project

  rag_managed_db_config {
    basic {}
  }

  depends_on = [google_project_service.apis]
}
