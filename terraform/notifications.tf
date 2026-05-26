locals {
  notification_email = [{ id = "adrien.kohlbecker@gmail.com" }]
}

resource "cloudflare_notification_policy" "passive_origin_monitoring" {
  account_id  = local.cloudflare_account_id
  alert_type  = "real_origin_monitoring"
  description = "Receive an email when your origin becomes unreachable"
  enabled     = true
  mechanisms = {
    email = local.notification_email
  }
  name = "Passive Origin Monitoring"
}
