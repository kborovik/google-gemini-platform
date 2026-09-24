locals {
  chat_image = "${var.region}-docker.pkg.dev/${var.project}/chat/handler:${data.archive_file.chat.output_md5}"
}

data "archive_file" "chat" {
  type             = "tar.gz"
  output_path      = "${path.module}/build/chat-handler.tar.gz"
  output_file_mode = "0644"

  source {
    content  = file("${path.module}/../chat/Dockerfile")
    filename = "Dockerfile"
  }

  source {
    content  = file("${path.module}/../chat/main.py")
    filename = "main.py"
  }

  source {
    content  = file("${path.module}/../chat/requirements.txt")
    filename = "requirements.txt"
  }
}

resource "google_storage_bucket" "chat_build" {
  name                        = "${var.project}-chat-build"
  location                    = var.region
  project                     = var.project
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_object" "chat_source" {
  name   = "source/chat.tar.gz"
  bucket = google_storage_bucket.chat_build.name
  source = data.archive_file.chat.output_path
}

resource "google_service_account" "chat_builder" {
  account_id   = "chat-builder"
  display_name = "Chat handler Cloud Build"
  project      = var.project

  depends_on = [google_project_service.apis]
}

# Cloud Build refuses the logs and staging bucket without storage.buckets.get.
# objectAdmin does not include that permission; storage.admin on this bucket does.
resource "google_storage_bucket_iam_member" "chat_builder_source" {
  bucket = google_storage_bucket.chat_build.name
  role   = "roles/storage.admin"
  member = google_service_account.chat_builder.member
}

resource "google_artifact_registry_repository_iam_member" "chat_builder" {
  project    = var.project
  location   = google_artifact_registry_repository.chat.location
  repository = google_artifact_registry_repository.chat.repository_id
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.chat_builder.member
}

resource "google_project_iam_member" "cloudbuild_service_agent" {
  project = var.project
  role    = "roles/cloudbuild.serviceAgent"
  member  = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

resource "google_service_account_iam_member" "cloudbuild_chat_builder" {
  service_account_id = google_service_account.chat_builder.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
}

# Provider has no resource that runs a build. Submit when chat/ changes.
# Logs stay in the build bucket so this stack does not enable Cloud Logging.
resource "terraform_data" "chat_image" {
  triggers_replace = data.archive_file.chat.output_md5

  depends_on = [
    google_storage_bucket_object.chat_source,
    google_storage_bucket_iam_member.chat_builder_source,
    google_artifact_registry_repository_iam_member.chat_builder,
    google_project_iam_member.cloudbuild_service_agent,
    google_service_account_iam_member.cloudbuild_chat_builder,
    google_artifact_registry_repository.chat,
  ]

  provisioner "local-exec" {
    command = join("\n", [
      "set -eu",
      "attempt=1",
      "while true; do",
      "  if gcloud builds submit \"gs://${google_storage_bucket.chat_build.name}/${google_storage_bucket_object.chat_source.name}\" --project=\"${var.project}\" --region=\"${var.region}\" --suppress-logs --config=\"${path.module}/../chat/cloudbuild.yaml\" --gcs-source-staging-dir=\"gs://${google_storage_bucket.chat_build.name}/staging\" --substitutions=_IMAGE=${local.chat_image},_LOGS_BUCKET=gs://${google_storage_bucket.chat_build.name},_SERVICE_ACCOUNT=projects/${var.project}/serviceAccounts/${google_service_account.chat_builder.email}; then",
      "    exit 0",
      "  fi",
      "  if [ \"$attempt\" -ge 3 ]; then",
      "    exit 1",
      "  fi",
      "  attempt=$((attempt + 1))",
      "  sleep 20",
      "done",
    ])
  }
}

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

    scaling {
      min_instance_count = 1
    }

    containers {
      image   = local.chat_image
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
    terraform_data.chat_image,
  ]
}

resource "google_cloud_run_domain_mapping" "chat" {
  location = var.region
  name     = "credit-policy.ai.lab5.ca"
  project  = var.project

  metadata {
    namespace = var.project
  }

  spec {
    route_name = google_cloud_run_v2_service.chat.name
  }

  depends_on = [
    google_project_service.apis,
    google_site_verification_web_resource.ai,
  ]
}
