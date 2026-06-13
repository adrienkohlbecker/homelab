# Off-fox CI orchestrator (notes/ci_aws_test_cells.md, "Path to 60" / Phase 1).
#
# fox (cpx22, 2 vCPU / 3.7 GiB) tops out at concurrent=16 — a full-universe
# pipeline (~145 cells) runs as ~9 sequential waves. This box exists to host the
# 60-wide fan-out instead: at concurrent=60 the same matrix is ~2.4 waves. The
# converge still runs on the remote EC2 cell (the orchestrator only drives ssh +
# boto3), so this is a fan-out *scheduler*, not where the heavy lifting lands.
#
# server_type ccx33: 8 *dedicated* vCPU / 32 GiB / 240 GiB local NVMe. Dedicated
# (no noisy-neighbour CPU steal) keeps the IO measurement clean, and it's the
# real shape we can provision — the Hetzner project's dedicated-core quota caps
# at 8, so ccx43 (16 vCPU, matching the experiment's c6a.4xlarge) needs a quota
# bump first. 8 cores is half that box, so the 60 container-starts ramp
# staggered rather than truly simultaneous; watch CPU *and* IO-PSI during the
# burst so a CPU-bound ramp (the throughput limiter, a sizing signal for prod)
# isn't misread as a clean IO result. The open question this box
# answers: the 2026-06-13 AWS experiment found a baseline-gp3 IOPS wall (60
# concurrent container-starts off the 3.27 GB CI image + ~145 checkouts saturated
# IO-PSI to 99%; 16000 provisioned IOPS was the load-bearing fix). Hetzner local
# NVMe should clear that wall for free — this box is where we PROVE it, by driving
# a real ROLES=ALL 60-burst and watching IO-PSI. Validate, don't assume.
#
# Auth/token: same HCLOUD_TOKEN op:// wiring as hetzner.tf (the `tf` task's
# `op run --` wrapper). Reuses hcloud_ssh_key.laptop registered there.

# Reserved public IPv4 — stable across rebuilds, and the /32 the homelab-ci-cell
# SG allowlists for orchestrator→cell SSH (aws_ci.tf). auto_delete = false so a
# server replace doesn't drop the address (and desync the SG / terraform state).
resource "hcloud_primary_ip" "orchestrator" {
  name        = "orchestrator"
  type        = "ipv4"
  location    = var.hetzner_location
  auto_delete = false
}

# Default-drop inbound; outbound open (no `out` rules), since the runner is
# pull-model — it dials gitlab.com, AWS APIs, and the cells *outbound*, and
# serves nothing (Prometheus metrics stay on loopback for netdata). The one
# inbound is SSH for Ansible, scoped to the home WAN like fox. If the home WAN
# IP changes, unblock via the Hetzner Cloud console and update the local.
resource "hcloud_firewall" "orchestrator" {
  name = "orchestrator"

  rule {
    description = "SSH for Ansible (home WAN only)"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["${local.home_wan_ip}/32"]
  }
}

# Stock Ubuntu 24.04 (noble): native tomllib, current LTS. No ZFS-root snapshot
# (that image is fox/headscale-specific); the orchestrator's root is plain ext4
# on the NVMe, which is exactly the surface the IO test wants to measure.
data "hcloud_image" "ubuntu_2404" {
  name              = "ubuntu-24.04"
  with_architecture = "x86"
}

resource "hcloud_server" "orchestrator" {
  name        = "orchestrator"
  server_type = "ccx33"
  image       = data.hcloud_image.ubuntu_2404.id
  location    = var.hetzner_location
  ssh_keys    = [hcloud_ssh_key.laptop.name]

  firewall_ids = [hcloud_firewall.orchestrator.id]

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.orchestrator.id
    ipv6_enabled = true
  }

  labels = {
    role = "ci-orchestrator"
    pool = "homelab"
  }

  lifecycle {
    ignore_changes = [user_data, ssh_keys]
  }

  # Boot with the fleet's `ak` user (sudo, key-only) so Ansible connects as `ak`
  # from first boot — the `user` role configures the existing login user, it
  # doesn't create it. Root SSH locked at the sshd level from first boot; the
  # `ssh` role's sshd_config takes over at converge. Mirrors hetzner.tf.
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
