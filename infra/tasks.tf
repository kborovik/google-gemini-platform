resource "google_service_account" "chat_tasks" {
  account_id   = "chat-tasks"
  display_name = "Chat tasks invoker"
  project      = var.project

  depends_on = [google_project_service.apis]
}

resource "google_cloud_tasks_queue" "chat" {
  name     = "${var.project}-chat"
  location = var.region
  project  = var.project

  depends_on = [google_project_service.apis]
}

# credit-policy-agent creates the task and must be allowed to act as chat-tasks.
resource "google_cloud_tasks_queue_iam_member" "agent_enqueuer" {
  project  = var.project
  location = google_cloud_tasks_queue.chat.location
  name     = google_cloud_tasks_queue.chat.name
  role     = "roles/cloudtasks.enqueuer"
  member   = google_service_account.agent.member
}

resource "google_service_account_iam_member" "agent_chat_tasks_user" {
  service_account_id = google_service_account.chat_tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.agent.member
}

# Cloud Tasks mints the OIDC token attached to POST /tasks/judge.
resource "google_service_account_iam_member" "cloudtasks_chat_tasks_user" {
  service_account_id = google_service_account.chat_tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
}
