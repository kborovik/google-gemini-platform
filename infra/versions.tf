terraform {
  required_version = ">= 1.10.0"

  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 8.1"
    }
  }

  # bucket + prefix come from `terraform init -backend-config`
  backend "gcs" {}
}

provider "google" {
  project = var.project
  region  = var.region

  # User ADC is rejected by Discovery Engine unless the quota project is set.
  billing_project       = var.project
  user_project_override = true
}
