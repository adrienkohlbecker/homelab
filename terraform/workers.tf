locals {
  # Route map: local-part-prefix -> destination addresses. The worker
  # parses recipient as `<prefix>[.<suffix>]@<domain>`, looks up <prefix>
  # in this map (case-insensitively), and forwards to each address.
  # Multi-destination aliases (cp.* -> both) live here, not in the JS.
  # Destination addresses come from the cloudflare_email_routing_address
  # resources in email.tf so the worker auto-tracks renames there.
  email_routes = {
    ak = [cloudflare_email_routing_address.adrien_gmail.email]
    sp = [cloudflare_email_routing_address.spouse_email.email]
    cp = [cloudflare_email_routing_address.adrien_gmail.email, cloudflare_email_routing_address.spouse_email.email]
  }
}

resource "cloudflare_workers_script" "catchall_email" {
  account_id  = local.cloudflare_account_id
  script_name = "catchall-email"
  content     = file("${path.module}/workers/catchall-email.js")
  # main_module flags the upload as ES-module-syntax (the JS uses
  # `export default {...}`). Without it the provider sends as a
  # classic service worker and CF rejects with 10021 / "Unexpected
  # token 'export'". The value is just a filename label; CF uses it
  # to identify the entry point within the upload bundle.
  main_module = "catchall-email.js"
  # Pinned to upload date (2024-08-02). Bump deliberately when changing
  # script behaviour, not casually -- new compat dates can flip JS
  # runtime semantics under the worker.
  compatibility_date = "2024-08-02"
  usage_model        = "standard"

  bindings = [{
    type = "json"
    name = "ROUTES"
    json = jsonencode(local.email_routes)
  }]
}
