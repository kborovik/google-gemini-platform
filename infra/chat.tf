resource "google_artifact_registry_repository" "chat" {
  project       = var.project
  location      = var.region
  repository_id = "chat"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service" "chat" {
  name                = "chat"
  location            = var.region
  project             = var.project
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    timeout         = "300s"
    service_account = google_service_account.agent.email

    containers {
      image   = "${var.region}-docker.pkg.dev/${var.project}/chat/handler"
      command = ["python", "main.py"]

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "REASONING_ENGINE"
        value = google_vertex_ai_reasoning_engine.credit_officer.name
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.chat,
  ]
}

# chat.lab5.ca DNS is a Cloudflare CNAME to ghs.googlehosted.com.
# This repo does not manage that zone.
resource "google_cloud_run_domain_mapping" "chat" {
  location = var.region
  name     = "chat.lab5.ca"
  project  = var.project

  metadata {
    namespace = var.project
  }

  spec {
    route_name = google_cloud_run_v2_service.chat.name
  }

  depends_on = [google_project_service.apis]
}
