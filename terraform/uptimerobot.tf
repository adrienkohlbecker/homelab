# UptimeRobot provides the external, off-estate half of monitoring: it watches
# the only surfaces reachable from the public internet. Uptime-Kuma and
# Healthchecks both run *inside* the estate (on lab), so they cannot observe --
# let alone alert on -- an outage that takes the estate itself offline. Anything
# that must page when home is down belongs here.
#
# Auth uses an account-scoped UptimeRobot API key (UI -> Integrations & API ->
# API keys). Stored in 1Password and surfaced via TF_VAR_uptimerobot_api_key,
# scoped to the `tf` task in mise.toml so it is only resolved under `op run --`.
#
# Surfaces intentionally NOT under tofu:
#
# - Alert contacts. Created and verified in the UI (each needs an out-of-band
#   confirmation click / device enrolment that tofu cannot drive). Read back
#   through the data source below so monitors subscribe to whatever exists.
#
# - Public status pages (PSPs) and maintenance windows. None configured; the
#   estate has no external audience for a status page.

variable "uptimerobot_api_key" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.uptimerobot_api_key) > 0
    error_message = "uptimerobot_api_key must be non-empty (resolved via TF_VAR_uptimerobot_api_key from 1Password through `op run`)."
  }
}

provider "uptimerobot" {
  api_key = var.uptimerobot_api_key
}

data "uptimerobot_alert_contacts" "active" {
  status = "active"

  # Asserted once, here, rather than per-resource: "the account has somewhere to
  # page" is a property of the data, not of any one monitor, so every monitor
  # added later inherits the guarantee. A `check` block would only warn; a
  # monitor that alerts nobody should halt the apply.
  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0
      error_message = "The UptimeRobot account has no active alert contacts; monitors would alert nobody."
    }
  }
}

locals {
  # Every monitor pages every active contact. Derived from the data source
  # rather than pinned by id: the account's contacts are managed in the UI (each
  # needs an out-of-band verification tofu cannot drive), so a literal list goes
  # stale silently -- a contact deleted and recreated keeps the monitor pointing
  # at a dead id, and a newly added channel is skipped until someone notices.
  # Adding a contact in the UI is the single control over who gets paged.
  uptimerobot_alert_contacts = [
    for contact_id in data.uptimerobot_alert_contacts.active.ids : {
      alert_contact_id = contact_id
      threshold        = 0
      recurrence       = 0
    }
  ]

  # One monitor per (endpoint x IP family): the split is what tells us *which*
  # path broke, since fox is dual-stack and the v6 leg has no in-estate observer
  # (neither the workstation nor lab has IPv6 egress).
  uptimerobot_monitors = {
    headscale_ipv4 = {
      name       = "Headscale IPV4"
      type       = "HTTP"
      url        = "https://headscale.fahm.fr/health"
      ip_version = "ipv4Only"
    }
    headscale_ipv6 = {
      name       = "Headscale IPV6"
      type       = "HTTP"
      url        = "https://headscale.fahm.fr/health"
      ip_version = "ipv6Only"
    }
    derp_ipv4 = {
      name                    = "Headscale DERP latency IPV4"
      type                    = "HTTP"
      url                     = "https://headscale.fahm.fr/derp/latency-check"
      ip_version              = "ipv4Only"
      response_time_threshold = 1000
    }
    derp_ipv6 = {
      name                    = "Headscale DERP latency IPV6"
      type                    = "HTTP"
      url                     = "https://headscale.fahm.fr/derp/latency-check"
      ip_version              = "ipv6Only"
      response_time_threshold = 1000
    }
  }
}

resource "uptimerobot_monitor" "headscale" {
  for_each = local.uptimerobot_monitors

  name     = each.value.name
  type     = each.value.type
  url      = each.value.url
  interval = 300
  timeout  = 30

  response_time_threshold = try(each.value.response_time_threshold, null)

  # No ssl_expiration_reminder / domain_expiration_reminder: the account's
  # UptimeRobot plan rejects both with 403 "not allowed to use some settings
  # with your current plan" (009-005), and the whole monitor update fails with
  # them set. check_ssl_errors still catches an already-invalid certificate;
  # advance warning of a stalled certbot renewal is simply not available here.
  check_ssl_errors    = true
  follow_redirections = false

  assigned_alert_contacts = local.uptimerobot_alert_contacts

  # fox is in Nuremberg; probing from other continents would blow the 1000ms
  # DERP threshold on round-trip alone and page with no signal about DERP health.
  region_data = {
    regions = ["eu"]
  }

  config = {
    ip_version = each.value.ip_version
  }
}

resource "uptimerobot_monitor" "resume" {
  name              = "Resume"
  type              = "KEYWORD"
  url               = "https://adrienkohlbecker.com"
  interval          = 300
  keyword_type      = "ALERT_NOT_EXISTS"
  keyword_case_type = "CaseSensitive"
  keyword_value     = "Strasbourg"
  timeout           = 30

  auth_type           = "NONE"
  follow_redirections = true

  # https, matching the zone's always_use_https = "on" (zone_settings.tf): over
  # http this measured a 301 rather than the served page, and could not express
  # an SSL check at all. No expiry reminder -- see the plan note above.
  check_ssl_errors = true

  assigned_alert_contacts = local.uptimerobot_alert_contacts

  region_data = {
    regions = ["eu"]
  }
}

# The two headscale endpoints collapsed into one for_each resource; these keep
# the already-applied monitors in place instead of destroying and recreating them.
moved {
  from = uptimerobot_monitor.headscale["ipv4"]
  to   = uptimerobot_monitor.headscale["headscale_ipv4"]
}

moved {
  from = uptimerobot_monitor.headscale["ipv6"]
  to   = uptimerobot_monitor.headscale["headscale_ipv6"]
}

moved {
  from = uptimerobot_monitor.headscale_derp["ipv4"]
  to   = uptimerobot_monitor.headscale["derp_ipv4"]
}

moved {
  from = uptimerobot_monitor.headscale_derp["ipv6"]
  to   = uptimerobot_monitor.headscale["derp_ipv6"]
}
