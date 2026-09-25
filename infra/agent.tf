# Standard ADK eleven. Terraform does not discover these from the package.
locals {
  adk_class_methods = [
    {
      name        = "get_session"
      api_mode    = ""
      description = "Retrieve session by ID"
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_get_session"
      api_mode    = "async"
      description = "Retrieve session asynchronously by ID"
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "list_sessions"
      api_mode    = ""
      description = "List all sessions for a user"
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_list_sessions"
      api_mode    = "async"
      description = "List all sessions for a user asynchronously"
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id = { type = "string" }
        }
      }
    },
    {
      name        = "create_session"
      api_mode    = ""
      description = "Create a new session"
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
          state      = { type = "object" }
        }
      }
    },
    {
      name        = "async_create_session"
      api_mode    = "async"
      description = "Create a new session asynchronously"
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
          state      = { type = "object" }
        }
      }
    },
    {
      name        = "delete_session"
      api_mode    = ""
      description = "Delete session by ID"
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_delete_session"
      api_mode    = "async"
      description = "Delete session asynchronously by ID"
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "stream_query"
      api_mode    = "stream"
      description = "Stream queries from the agent"
      parameters = {
        type     = "object"
        required = ["message", "user_id"]
        properties = {
          message    = { description = "Message string or object" }
          user_id    = { type = "string" }
          session_id = { type = "string" }
          run_config = { type = "object" }
        }
      }
    },
    {
      name        = "async_stream_query"
      api_mode    = "async_stream"
      description = "Stream queries asynchronously from the agent"
      parameters = {
        type     = "object"
        required = ["message", "user_id"]
        properties = {
          message        = { description = "Message string or object" }
          user_id        = { type = "string" }
          session_id     = { type = "string" }
          session_events = { type = "array", items = { type = "object" } }
          run_config     = { type = "object" }
        }
      }
    },
    {
      name        = "streaming_agent_run_with_events"
      api_mode    = "async_stream"
      description = "Stream agent run with events asynchronously"
      parameters = {
        type     = "object"
        required = ["request_json"]
        properties = {
          request_json = { type = "string" }
        }
      }
    },
  ]
}

data "archive_file" "credit_officer" {
  type             = "tar.gz"
  output_path      = "${path.module}/build/credit_officer.tar.gz"
  output_file_mode = "0644"

  source {
    content  = file("${path.module}/../agents/credit_officer/agent.py")
    filename = "agent.py"
  }

  source {
    content  = file("${path.module}/../agents/credit_officer/credit-policy-agent.instructions.md")
    filename = "credit-policy-agent.instructions.md"
  }

  source {
    content  = file("${path.module}/../agents/credit_officer/requirements.txt")
    filename = "requirements.txt"
  }
}

resource "google_vertex_ai_reasoning_engine" "credit_officer" {
  project      = var.project
  region       = var.region
  display_name = "credit-officer"
  description  = "Contoso Demo Bank credit officer"

  spec {
    agent_framework = "google-adk"
    class_methods   = jsonencode(local.adk_class_methods)
    service_account = google_service_account.agent.email

    source_code_spec {
      inline_source {
        source_archive = filebase64(data.archive_file.credit_officer.output_path)
      }

      python_spec {
        entrypoint_module = "agent"
        entrypoint_object = "root_agent"
        requirements_file = "requirements.txt"
        version           = "3.14"
      }
    }

    deployment_spec {
      env {
        name  = "DATA_STORE"
        value = google_discovery_engine_data_store.kb_credit_policies.name
      }
      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.agent_aiplatform,
    google_project_iam_member.agent_discoveryengine,
    google_project_iam_member.agent_traces,
    google_project_iam_member.agent_logs,
    google_storage_bucket_iam_member.agent_viewer,
    google_service_account_iam_member.reasoning_engine_service_agent_user,
    google_service_account_iam_member.reasoning_engine_service_agent_token_creator,
  ]
}
