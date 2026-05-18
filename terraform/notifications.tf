resource "cloudflare_notification_policy" "passive_origin_monitoring" {
  account_id  = data.cloudflare_account.main.account_id
  alert_type  = "real_origin_monitoring"
  description = "Recieve an email when your origin becomes unreachable"
  enabled     = true
  mechanisms = {
    email = [
      {
        id = "adrien.kohlbecker@gmail.com"
      },
    ]
    pagerduty = null
    webhooks  = null
  }
  name = "Passive Origin Monitoring"
}
