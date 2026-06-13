# Off-fox CI orchestrator (notes/ci_aws_test_cells.md, "Path to 60" / Phase 1).
#
# fox (cpx22, 2 vCPU / 3.7 GiB) tops out at concurrent=16 — a full-universe
# pipeline (~145 cells) runs as ~9 sequential waves. This box exists to host the
# 60-wide fan-out instead: at concurrent=60 the same matrix is ~2.4 waves. The
# converge still runs on the remote EC2 cell (the orchestrator only drives ssh +
# boto3), so this is a fan-out *scheduler*, not where the heavy lifting lands.
#
# server_type: the orchestrator box under benchmark. The ccx33 run PROVED Hetzner
# local NVMe clears the baseline-gp3 IOPS wall outright — the 60-wide
# container-start burst peaked at IO-PSI ~6% (full) vs gp3's 99% collapse — and
# that CPU, not disk, is the limiter: each cell's `uv sync` venv build plus the
# ansible controller both run on the orchestrator (~one per cell). So the open
# question is now purely how many cores a *keep-running* box needs. We benchmark
# the affordable shared types against the 60-wide full-universe fan-out, watching
# CPU-PSI and %st (steal). Currently: cx43 (shared vCPU). See
# notes/ci_aws_test_cells.md (Path to 60).
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
  server_type = "cx43"
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

  # Boot with the fleet's `ak` user (sudo, key-only) so SSH connects as `ak`
  # from first boot. Root SSH locked at the sshd level. The runcmd block then
  # provisions the box into a ready-to-register CI orchestrator: docker.io (the
  # executor), gitlab-runner (the runner itself), and mise — the same three
  # installs done by hand for the first ccx33 test, baked here so the box can be
  # torn down and rebuilt at a different server_type (cx43 / cpx42 / ...) with one
  # `tofu apply` for repeated CPU/throughput benchmark runs. Runner
  # REGISTRATION stays manual: the glrt- token is a secret and must never land
  # in committed user_data — after a rebuild, `gitlab-runner register` + set
  # concurrent=60 (see notes/ci_aws_test_cells.md, Path to 60). ignore_changes
  # on user_data below means editing this never disturbs a running box; it only
  # takes effect on a deliberate taint/replace.
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
      - [bash, -c, "curl -sL https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh | bash"]
      - [bash, -c, "install -dm755 /etc/apt/keyrings && wget -qO- https://mise.jdx.dev/gpg-key.pub | gpg --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg && echo 'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg arch=amd64] https://mise.jdx.dev/deb stable main' > /etc/apt/sources.list.d/mise.list"]
      - [apt-get, update]
      - [apt-get, install, -y, docker.io, gitlab-runner, mise]
      - [usermod, -aG, docker, gitlab-runner]
  EOT
}
