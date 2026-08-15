provider "uptimerobot" {}

data "uptimerobot_alert_contacts" "active" {
  status = "active"
}

locals {
  headscale_uptimerobot_monitors = {
    ipv4 = "ipv4Only"
    ipv6 = "ipv6Only"
  }

  headscale_uptimerobot_alert_contacts = [
    for contact_id in data.uptimerobot_alert_contacts.active.ids : {
      alert_contact_id = contact_id
      threshold        = 0
      recurrence       = 0
    }
  ]
}

resource "uptimerobot_monitor" "headscale" {
  for_each = local.headscale_uptimerobot_monitors

  name              = "Headscale ${upper(each.key)}"
  type              = "KEYWORD"
  url               = "https://headscale.fahm.fr/health"
  interval          = 300
  keyword_type      = "ALERT_EXISTS"
  keyword_case_type = "CaseSensitive"
  keyword_value     = "\"status\":\"pass\""
  timeout           = 30

  check_ssl_errors    = true
  follow_redirections = false

  assigned_alert_contacts = local.headscale_uptimerobot_alert_contacts

  config = {
    ip_version = each.value
  }

  lifecycle {
    precondition {
      condition     = length(data.uptimerobot_alert_contacts.active.ids) > 0
      error_message = "The UptimeRobot account has no active alert contacts."
    }
  }
}

resource "uptimerobot_monitor" "headscale_derp" {
  for_each = local.headscale_uptimerobot_monitors

  name                    = "Headscale DERP latency ${upper(each.key)}"
  type                    = "HTTP"
  url                     = "https://headscale.fahm.fr/derp/latency-check"
  interval                = 300
  timeout                 = 30
  response_time_threshold = 1000

  check_ssl_errors    = true
  follow_redirections = false

  assigned_alert_contacts = local.headscale_uptimerobot_alert_contacts

  config = {
    ip_version = each.value
  }

  lifecycle {
    precondition {
      condition     = length(data.uptimerobot_alert_contacts.active.ids) > 0
      error_message = "The UptimeRobot account has no active alert contacts."
    }
  }
}
