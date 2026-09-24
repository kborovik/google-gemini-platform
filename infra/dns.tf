# Parent name servers for ai.lab5.ca stay in zone lab5.ca.
# This repo does not manage that zone.
resource "google_dns_managed_zone" "ai" {
  name        = "ai-lab5-ca"
  dns_name    = "ai.lab5.ca."
  description = "Public zone for the credit-policy Chat host."
  project     = var.project
  visibility  = "public"

  depends_on = [google_project_service.apis]
}

resource "google_dns_record_set" "credit_policy" {
  project      = var.project
  managed_zone = google_dns_managed_zone.ai.name
  name         = "credit-policy.ai.lab5.ca."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["ghs.googlehosted.com."]
}
