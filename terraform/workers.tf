resource "cloudflare_workers_script" "catchall_email" {
  account_id  = data.cloudflare_account.main.account_id
  script_name = "catchall-email"
  content     = file("${path.module}/workers/catchall-email.js")
  # Pinned to upload date (2024-08-02). Bump deliberately when changing
  # script behaviour, not casually -- new compat dates can flip JS
  # runtime semantics under the worker.
  compatibility_date = "2000-01-01"
  usage_model        = "standard"
}
