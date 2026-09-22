terraform {
  required_version = ">= 1.10.0"

  required_providers {
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
}
