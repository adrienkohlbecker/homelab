# Holds the Google OAuth 2.0 client backing
# cloudflare_zero_trust_access_identity_provider.google (access.tf).
# The client itself is console-only -- Google exposes no public API
# for generic OAuth 2.0 client IDs and no terraform resource exists;
# values flow in via var.google_idp_client_id / _secret from 1Password.

# Auth via ADC (~/.config/gcloud/application_default_credentials.json).
provider "google" {
  # Literal, not google_project.cloudflare_access.project_id -- a
  # reference creates a provider <-> resource cycle on import.
  project = "cloudflare-access-220902"
}

resource "google_project" "cloudflare_access" {
  name       = "Cloudflare Access"
  project_id = "cloudflare-access-220902"

  # The OAuth client inside this project is unrecreatable via API;
  # destroying the project orphans it permanently.
  lifecycle {
    prevent_destroy = true
  }
}

# _binding (authoritative for this role only), not _policy: a future
# API enablement may bind a Google-managed service agent in another
# role, and terraform leaves those alone.
resource "google_project_iam_binding" "owner" {
  project = google_project.cloudflare_access.project_id
  role    = "roles/owner"
  members = ["user:adrien.kohlbecker@gmail.com"]
}
