# Scaleway hosts the off-home `fox` VPS: the only internet-facing surface of
# the homelab (a Stardust1-S running Headscale + its embedded DERP relay, see
# roles/headscale and notes/headscale_mesh_redesign.md). The home hosts dial
# *outbound* to it, so nothing at home is exposed.
#
# Credentials come from the scw CLI config (~/.config/scw/config.yaml, active
# profile) -- the provider auto-loads it, so there's nothing to wire in
# mise.toml [env]. Run `scw init` once (mints/stores an API key + sets the
# default project/organization) before `tofu plan`. op run leaves the file
# read untouched, so the mise `tf` wrapper still picks it up.
#
# The operator's SSH public key must be registered in the Scaleway project
# (IAM > SSH keys); Scaleway's cloud-init injects every project key into the
# instance's default user, which is how Ansible first reaches the host. User
# provisioning (the `ak` account, hardening) is then Ansible's job.
# No region/zone here: setting them in the provider block while the scw
# profile (config.yaml) also defines them trips the provider's "multiple
# variable sources" warning. Pin the zone per-resource instead (via
# var.scaleway_zone) so placement is deterministic regardless of the profile
# default.
provider "scaleway" {}

variable "scaleway_zone" {
  description = "Scaleway zone for the fox VPS resources."
  type        = string
  default     = "fr-par-1"
}

# The operator's laptop SSH key, registered in the Scaleway project so the
# instance's cloud-init injects it into the default user (Ansible's first
# hop). Mirrors the `laptop` entry in group_vars/all/main.yml's
# ssh_public_keys -- kept as a literal here since public keys aren't secret
# and terraform doesn't read ansible group_vars.
resource "scaleway_iam_ssh_key" "laptop" {
  name       = "laptop"
  public_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQC/x/7HjVzMzqx9r8sRtZxgviFo7V35svZXaZAKGd6BJLUS+qwYreSRCkyjJHmwFyDyUR2sOJmo69weV3XYs0GOvL72t23czWUDDU/fXQWmIcWHPyU+nUEN3VKHgh5zed002ACEivTDUvSjmprBGSx5YZNfZjnqtd+X+kVojwI5BTWyQMNJGSAPf/I7Zdt01d8Klq5eKs30QAdMCiwQ7qyME31gk7dbWVrcf1tc4VCbKbL0co1dj3A5tRe6TtZ/OObj5EHj0UZNmG16PY9cbi3kkwZ5Wxb2e6LHelgUjWn7a1OGRSox5IkTjDNUJ/71p+qoYGjH7V+UtvUBx2f3gG2A4oeduUpthfDUjDW4Rii43miMZJ2OAH95nY0NtDTdek6ZHqMwyqIEZdxV3QiDO1qIeCViJ3xBn3xHJb4oZs0nTOugrlcDXziQ5bfbvkMUpGkkM26+/S1iaA/rtel40P70obZx07s0SA3wTREBurP+wd7mrpp2rmpyqLlWygyorf9DMPMpj0YeAuPV5hl/qQdM0qVG+u+leY4GzrdO69vh3rI7edTROlkzSTmaKfyZ8t71O/0i3y+GsQxVx3z62zvgQ0chvUIsUSWUOGFZDBLM0X7z9MddGqAqaf5MnkQ6NEdE4hYKXtg+u0vYcfwjGO06/Rbc0/V9y68OZoJxMeqsXw== laptop"
}

# Reserved public IPv4 -- stable across instance rebuilds and is what
# fox.fahm.fr points at (see dns_fahm_fr.tf).
resource "scaleway_instance_ip" "fox" {
  zone = var.scaleway_zone
  type = "routed_ipv4"
}

# Default-drop inbound. Only the mesh control plane is reachable from the
# internet: 443/tcp (Headscale control API + DERP relay, TLS-terminated by
# nginx) and 3478/udp (embedded DERP STUN for NAT traversal). 51820/udp is the
# wg0 backbone: fox is a public listener and the home hosts dial OUT to it, so
# the home end opens no inbound. SSH is open for Ansible -- key-only + fail2ban
# on the host is the repo's posture; tighten to the home WAN IP (203.0.113.10)
# if you never administer fox from elsewhere.
resource "scaleway_instance_security_group" "fox" {
  zone                    = var.scaleway_zone
  name                    = "fox"
  description             = "headscale control plane (managed by terraform)"
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"

  inbound_rule {
    action   = "accept"
    protocol = "TCP"
    port     = 443
  }

  inbound_rule {
    action   = "accept"
    protocol = "UDP"
    port     = 3478
  }

  inbound_rule {
    action   = "accept"
    protocol = "TCP"
    port     = 22
  }

  # NOTE: scaleway_instance_security_group inbound_rule blocks are positional,
  # so new rules must be APPENDED here. Inserting above an existing rule makes
  # terraform rewrite that rule in place (e.g. flip the live SSH rule to wg)
  # and append a replacement -- a transient drop of the rewritten rule.
  inbound_rule {
    action   = "accept"
    protocol = "UDP"
    port     = 51820
  }
}

resource "scaleway_instance_server" "fox" {
  zone              = var.scaleway_zone
  name              = "fox"
  type              = "STARDUST1-S"
  image             = "ubuntu_jammy"
  ip_id             = scaleway_instance_ip.fox.id
  security_group_id = scaleway_instance_security_group.fox.id
  tags              = ["headscale", "homelab"]

  # Stardust1-S ships a small local root volume; 20 GB is ample for the OS +
  # Headscale's SQLite DB and keys. Adjust volume_type/size if the provider
  # rejects it for this instance type.
  root_volume {
    size_in_gb = 20
  }
}
