variable "project" {
  type        = string
  description = "GCP project id. This stack runs in lab5-gemini-dev1."
}

variable "region" {
  type        = string
  description = "GCP region for the document bucket and Agent Runtime."
}
