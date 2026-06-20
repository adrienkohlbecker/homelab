# Hetzner Cloud hosts the off-home `fox` VPS: the only internet-facing surface
# of the homelab (a CX23 running Headscale + its embedded DERP relay, see
# roles/headscale and notes/headscale_mesh_redesign.md). The home hosts dial
# *outbound* to it, so nothing at home is exposed.
#
# Auth: the hcloud provider reads HCLOUD_TOKEN from the environment (it does
# NOT auto-load the hcloud CLI's ~/.config/hcloud/cli.toml the way the CLI and
# the packer/audit tooling do). The token is wired into mise.toml [env] as the
# HCLOUD_TOKEN_OP op:// ref, mapped to HCLOUD_TOKEN by the `tf` task's
# `op run --` wrapper -- terraform is the only consumer still needing it.
#
# Created from scratch by `tofu apply` (no import) -- every resource below is
# terraform-managed end to end. The old hand-made Hetzner instance is being
# deleted out of band first so there's no name clash on `fox`.
provider "hcloud" {}

variable "hetzner_location" {
  description = "Hetzner location for the fox VPS."
  type        = string
  default     = "nbg1"
}

variable "hetzner_network_zone" {
  description = "Hetzner network zone the private subnet lives in (eu-central covers nbg1/fsn1/hel1)."
  type        = string
  default     = "eu-central"
}

# The operator's laptop SSH key, registered in the project so the instance's
# cloud-init injects it into the `ak` user (Ansible's first hop).
resource "hcloud_ssh_key" "laptop" {
  name       = "laptop"
  public_key = local.operator_ssh_public_key
}

# Reserved public IPv4 -- stable across instance rebuilds and is what
# fox.fahm.fr points at (see dns_fahm_fr.tf). auto_delete = false so deleting
# the server doesn't drop the IP (and corrupt the terraform state).
resource "hcloud_primary_ip" "fox" {
  name        = "fox"
  type        = "ipv4"
  location    = var.hetzner_location
  auto_delete = false
}

# Reserved public IPv6 -- same rationale as the IPv4 above. fox is a dual-stack
# DERP relay + control plane (fox.fahm.fr carries both A and AAAA, see
# dns_fahm_fr.tf), so IPv6-only tailnet clients reach the relay over 443/nginx
# and the embedded DERP STUN over 3478. ip_address is a single usable address
# (Hetzner also hands out the surrounding /64 as ip_network); the AAAA points at
# ip_address. Reserving it (rather than the server's auto-assigned IPv6) keeps
# the address stable across rebuilds so the AAAA needn't churn.
resource "hcloud_primary_ip" "fox_v6" {
  name        = "fox_v6"
  type        = "ipv6"
  location    = var.hetzner_location
  auto_delete = false
}

# Default-drop inbound (an attached hcloud firewall drops anything not
# explicitly allowed; outbound stays open with no `out` rules). Three ports stay
# world-open because every tailnet client hits them from arbitrary IPs: 443/tcp
# (Headscale control API + DERP relay, TLS-terminated by nginx) and 3478/udp
# (embedded DERP STUN for NAT traversal) -- both load-bearing since the DERP map
# is embedded-only (no third-party relay fallback) -- plus 41641/udp (tailscaled
# WireGuard underlay), so clients using fox as an exit node / subnet router reach
# its tailscaled DIRECTLY instead of hairpinning through fox's own embedded DERP.
# 51820/udp (wg0 backbone) and 22/tcp (SSH) are scoped to the
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
    description = "tailscale direct (fox as exit node / subnet router)"
    direction   = "in"
    protocol    = "udp"
    port        = "41641"
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

# fox's boot image: the latest ZFS-root snapshot published by `mise run
# packer:hetzner`, which uploads the disk image `mise run packer:build hetzner`
# produces (mechanic 2 -- see notes/fox_zfs_root_image.md). The build labels
# each snapshot os=ubuntu-zfs,ubuntu=<release>; we select on os + release (NOT a
# per-host role) so the same image can back multiple Hetzner servers later.
# most_recent picks the newest, so a rebuild is consumed automatically on the
# next (re)create; with_status=available avoids a half-published snapshot.
# NB: paired with ignore_changes=[image] on the server below — a freshly built
# snapshot does NOT auto-recreate the running fox (recreate wipes the disk +
# headscale state); it only takes effect on a deliberate taint/replace.
data "hcloud_image" "fox" {
  with_selector     = "os=ubuntu-zfs,ubuntu=noble"
  with_architecture = "x86"
  with_status       = ["available"]
  most_recent       = true
}

resource "hcloud_server" "fox" {
  name        = "fox"
  server_type = "cpx22"
  image       = data.hcloud_image.fox.id
  location    = var.hetzner_location
  ssh_keys    = [hcloud_ssh_key.laptop.name]

  firewall_ids = [hcloud_firewall.fox.id]

  # No server-side delete_protection: Hetzner's `change_protection` requires the
  # delete and rebuild flags to be equal, so enabling delete protection would
  # also enable rebuild_protection -- and an in-place `rebuild` is the only way
  # to push a freshly built ZFS-root image onto fox WITHOUT a destroy+recreate
  # that would re-trigger Hetzner's June 2026 price adjustment. Keeping rebuild
  # unblocked matters more than guarding console/API deletes, so destroy safety
  # lives entirely in the terraform lifecycle block below (prevent_destroy).

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.fox.id
    ipv6_enabled = true
    ipv6         = hcloud_primary_ip.fox_v6.id
  }

  labels = {
    role = "headscale"
    pool = "homelab"
  }

  lifecycle {
    # prevent_destroy: terraform errors at plan time on anything that would
    # destroy fox -- an explicit `terraform destroy`, or a replacement forced
    # by some ForceNew attribute. A destroy+recreate would re-provision fox as a
    # NEW order, locking in the post-15-Jun-2026 CPX22 price (EUR 7.99 -> 19.49)
    # and wiping the headscale control-plane state. server_type in
    # ignore_changes makes terraform never issue a rescale (change_type) on
    # server_type drift -- a rescale is the other action that re-triggers the
    # new pricing. To deliberately rescale (e.g. once fox is no longer
    # grandfathered), drop server_type from this list AND edit it above; to
    # delete, remove prevent_destroy first.
    prevent_destroy = true
    ignore_changes  = [user_data, ssh_keys, image, server_type]
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
