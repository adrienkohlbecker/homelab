# Hetzner Cloud hosts the off-home `fox` VPS: the only internet-facing surface
# of the homelab (a CX23 running Headscale + its embedded DERP relay, see
# roles/headscale and notes/headscale_mesh_redesign.md). The home hosts dial
# *outbound* to it, so nothing at home is exposed.
#
# Auth: the hcloud provider reads HCLOUD_TOKEN from the environment (it does
# NOT auto-load the hcloud CLI's ~/.config/hcloud/cli.toml the way the scaleway
# provider read its config). The token is wired into mise.toml [env] as an
# op:// ref, resolved by the `tf` task's `op run --` wrapper.
#
# Created from scratch by `tofu apply` (no import) -- every resource below is
# terraform-managed end to end. The old hand-made Hetzner instance is being
# deleted out of band first so there's no name clash on `fox`.
provider "hcloud" {}

variable "hetzner_location" {
  description = "Hetzner location for the fox VPS (hel1 -- nbg1/fsn1 had no available instance)."
  type        = string
  default     = "hel1"
}

variable "hetzner_network_zone" {
  description = "Hetzner network zone the private subnet lives in (eu-central covers nbg1/fsn1/hel1)."
  type        = string
  default     = "eu-central"
}

# The operator's laptop SSH key, registered in the project so the instance's
# cloud-init injects it into the `ak` user (Ansible's first hop). Mirrors the
# `laptop` entry in group_vars/all/main.yml's ssh_public_keys -- kept as a
# literal here since public keys aren't secret and terraform doesn't read
# ansible group_vars.
resource "hcloud_ssh_key" "laptop" {
  name       = "laptop"
  public_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQC/x/7HjVzMzqx9r8sRtZxgviFo7V35svZXaZAKGd6BJLUS+qwYreSRCkyjJHmwFyDyUR2sOJmo69weV3XYs0GOvL72t23czWUDDU/fXQWmIcWHPyU+nUEN3VKHgh5zed002ACEivTDUvSjmprBGSx5YZNfZjnqtd+X+kVojwI5BTWyQMNJGSAPf/I7Zdt01d8Klq5eKs30QAdMCiwQ7qyME31gk7dbWVrcf1tc4VCbKbL0co1dj3A5tRe6TtZ/OObj5EHj0UZNmG16PY9cbi3kkwZ5Wxb2e6LHelgUjWn7a1OGRSox5IkTjDNUJ/71p+qoYGjH7V+UtvUBx2f3gG2A4oeduUpthfDUjDW4Rii43miMZJ2OAH95nY0NtDTdek6ZHqMwyqIEZdxV3QiDO1qIeCViJ3xBn3xHJb4oZs0nTOugrlcDXziQ5bfbvkMUpGkkM26+/S1iaA/rtel40P70obZx07s0SA3wTREBurP+wd7mrpp2rmpyqLlWygyorf9DMPMpj0YeAuPV5hl/qQdM0qVG+u+leY4GzrdO69vh3rI7edTROlkzSTmaKfyZ8t71O/0i3y+GsQxVx3z62zvgQ0chvUIsUSWUOGFZDBLM0X7z9MddGqAqaf5MnkQ6NEdE4hYKXtg+u0vYcfwjGO06/Rbc0/V9y68OZoJxMeqsXw== laptop"
}

# Reserved public IPv4 -- stable across instance rebuilds and is what
# fox.fahm.fr points at (see dns_fahm_fr.tf). auto_delete = false so deleting
# the server doesn't drop the IP (and corrupt the terraform state).
resource "hcloud_primary_ip" "fox" {
  name          = "fox"
  type          = "ipv4"
  location      = var.hetzner_location
  auto_delete   = false
}

# Default-drop inbound (an attached hcloud firewall drops anything not
# explicitly allowed; outbound stays open with no `out` rules). Only the mesh
# control plane is reachable from the internet: 443/tcp (Headscale control API
# + DERP relay, TLS-terminated by nginx) and 3478/udp (embedded DERP STUN for
# NAT traversal) -- both MUST stay world-open, since every tailnet client hits
# them from arbitrary IPs and the DERP map is embedded-only (no third-party
# relay fallback). 51820/udp (wg0 backbone) and 22/tcp (SSH) are scoped to the
# home WAN IP only (local.home_wan_ip): fox's only wg0 initiators are lab+pug
# (keepalive_from: [lab, pug]), both behind the home WAN, and fox is
# administered from home -- so neither needs internet exposure. If the home WAN
# IP changes, unblock via the Hetzner Cloud console and update the local.
resource "hcloud_firewall" "fox" {
  name = "fox"

  rule {
    description = "headscale control API + DERP relay (nginx TLS)"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "embedded DERP STUN (NAT traversal)"
    direction   = "in"
    protocol    = "udp"
    port        = "3478"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "SSH for Ansible (home WAN only)"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["${local.home_wan_ip}/32"]
  }

  rule {
    description = "wg0 backbone listener (home WAN only)"
    direction   = "in"
    protocol    = "udp"
    port        = "51820"
    source_ips  = ["${local.home_wan_ip}/32"]
  }
}

# Private network for the `hetzner` site (data/network_topology.yml). fox gets
# 10.123.55.2 on it; the +64.0.0 wireguard mirror is 10.123.119.2. The home
# hosts aren't on this network -- it's the canonical anchor the topology's
# physical->wireguard derivation hangs off, and a private interface fox itself
# can use. ip_range / subnet both read from the topology so there's one source
# of truth (cf. the DNS records in dns_fahm_fr.tf).
resource "hcloud_network" "hetzner" {
  name     = "hetzner"
  ip_range = local.network.sites.hetzner.cidr
}

resource "hcloud_network_subnet" "hetzner" {
  type         = "cloud"
  network_id   = hcloud_network.hetzner.id
  network_zone = var.hetzner_network_zone
  ip_range     = local.network.sites.hetzner.cidr
}

resource "hcloud_server" "fox" {
  name        = "fox"
  server_type = "cx23"
  image       = "ubuntu-22.04"
  location    = var.hetzner_location
  ssh_keys    = [hcloud_ssh_key.laptop.name]

  firewall_ids = [hcloud_firewall.fox.id]

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.fox.id
    ipv6_enabled = true
  }

  labels = {
    role = "headscale"
    pool = "homelab"
  }

  # Boot with the fleet's regular `ak` user (sudo, key-only) instead of logging
  # in as root: Ansible connects as `ak` from first boot (the `user` role only
  # *configures* the existing login user, it doesn't create it). The SSH key is
  # the same laptop key registered above. Root SSH is locked at the sshd level
  # from first boot via the drop-in below; the `ssh` role's sshd_config (also
  # PermitRootLogin no) takes over at converge. The `ssh_root` role (root's
  # outbound keypair) is intentionally not in fox's play.
  user_data = <<-EOT
    #cloud-config
    disable_root: true
    package_update: true
    package_upgrade: true
    users:
      - name: ak
        groups: [sudo]
        shell: /bin/bash
        sudo: "ALL=(ALL) NOPASSWD:ALL"
        lock_passwd: true
        ssh_authorized_keys:
          - ${hcloud_ssh_key.laptop.public_key}
    write_files:
      - path: /etc/ssh/sshd_config.d/10-disable-root.conf
        content: |
          PermitRootLogin no
    runcmd:
      - [systemctl, restart, ssh]
  EOT
}

# Separate attachment (preferred over an inline server `network {}` block --
# avoids the 1.4+ detach/attach-on-every-apply bug and imports cleanly as
# "<server-id>-<network-id>"). fox's private IP is the topology's physical
# address for the host.
resource "hcloud_server_network" "fox" {
  server_id = hcloud_server.fox.id
  subnet_id = hcloud_network_subnet.hetzner.id
  ip        = local.network.hosts.fox.physical
}
