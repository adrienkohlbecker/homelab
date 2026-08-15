provider "uptimerobot" {}

data "uptimerobot_alert_contacts" "active" {
  status = "active"
}

locals {
  headscale_uptimerobot_monitors = {
    ipv4 = "ipv4Only"
    ipv6 = "ipv6Only"
  }
}

resource "uptimerobot_monitor" "headscale" {
  for_each = local.headscale_uptimerobot_monitors

  name     = "Headscale ${upper(each.key)}"
  type     = "API"
  url      = "https://headscale.fahm.fr/health"
  interval = 300
  timeout  = 30

  check_ssl_errors    = true
  follow_redirections = false

  assigned_alert_contacts = [
    for contact_id in data.uptimerobot_alert_contacts.active.ids : {
      alert_contact_id = contact_id
      threshold        = 0
      recurrence       = 0
    }
  ]

  config = {
    ip_version = each.value
    api_assertions = {
      logic = "AND"
      checks = [{
        property   = "$.status"
        comparison = "equals"
        target     = jsonencode("pass")
      }]
    }
  }

  lifecycle {
    precondition {
      condition     = length(data.uptimerobot_alert_contacts.active.ids) > 0
      error_message = "The UptimeRobot account has no active alert contacts."
    }
  }
}
