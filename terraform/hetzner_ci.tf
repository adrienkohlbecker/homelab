# Dedicated Hetzner Cloud project for the ephemeral CI worker fleet
# ("homelab-ci"). Hetzner has no API for project creation, so the project
# itself is made once by hand in the Cloud Console; everything INSIDE it is
# terraform-managed here through the aliased provider below.
#
# Why a separate project: CI workers are root-SSH VMs running job-controlled
# code, and the gitlab_runner manager on fox holds this project's API token in
# its config.toml (roles/gitlab_runner). If that token leaks, the blast radius
# must stop at the disposable CI fleet and its snapshots -- never reach
# fox/home, which stay in the main project under the main token. Design:
# notes/ci_ephemeral_hetzner_workers.md.
#
# What lives in the project: the ci_worker + ci_builder firewalls below, the
# role=ci-worker worker snapshots baked by `mise run packer:worker`, and the
# ephemeral servers the fleeting plugin provisions at runtime (plugin-owned,
# never terraform-managed).
#
# Bootstrap (manual, once):
#   1. Cloud Console -> create project "homelab-ci".
#   2. In that project: Security -> API tokens -> generate a read/write token.
#   3. Store it in 1Password and update the two op:// item-ids in mise.toml
#      (TF_VAR_hcloud_ci_token feeds this provider; HCLOUD_TOKEN_CI_OP feeds
#      `mise run packer:worker`). Until then `op run` fails on the
#      placeholder refs, which blocks `mise run tf` -- deliberate: nothing in
#      this file can apply without the project anyway.
#   4. Scope-check before trusting it anywhere: `HCLOUD_TOKEN=<new> hcloud
#      all list` must return only CI-project resources -- a wrong-project
#      token converges and works silently.
#   5. At activation, vault the same token into host_vars/fox.yml as
#      gitlab_runner_hcloud_token.
variable "hcloud_ci_token" {
  description = "Read/write API token for the dedicated homelab-ci Hetzner project."
  type        = string
  sensitive   = true
}

provider "hcloud" {
  alias = "ci"
  token = var.hcloud_ci_token
}

# SSH-only firewall for the ephemeral CI worker fleet at runtime.
# Default-drop inbound with only 22/tcp open to the fleet's two operators:
# the home WAN (manual debugging) and fox (the gitlab_runner fleeting
# manager SSHes into workers over their public IPs -- fox itself lives in
# the main project, so its address crosses projects here as a plain IP
# literal). The label selector auto-applies it to every instance the
# fleeting plugin creates (stamped role=ci by roles/gitlab_runner's
# config.toml) -- the plugin itself has no firewall support, so without the
# selector each worker's root sshd would sit world-open for its whole warm
# window.
resource "hcloud_firewall" "ci_worker" {
  provider = hcloud.ci
  name     = "ci-worker"

  rule {
    description = "SSH (home WAN + fox)"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips = [
      "${local.home_wan_ip}/32",
      "${hcloud_primary_ip.fox.ip_address}/32",
    ]
  }

  apply_to {
    label_selector = "role=ci"
  }
}

# Firewall for the packer build VM that bakes worker snapshots
# (packer/hcloud_worker.pkr.hcl attaches it by name; no label selector --
# nothing should pick this up automatically). SSH stays world-open because
# the bake runs from gitlab.com hosted runners, whose egress IPs are
# dynamic Google Cloud addresses with no published range -- allowlisting
# "GCP us-east1" would admit anyone with a rented VM there while adding a
# maintenance treadmill. Accepted exposure: key-only auth (packer injects
# an ephemeral ed25519 key, cloud images ship no root password) on a
# packer-managed VM that lives ~15 minutes and runs no job payloads.
resource "hcloud_firewall" "ci_builder" {
  provider = hcloud.ci
  name     = "ci-builder"

  rule {
    description = "SSH (packer from gitlab.com hosted runners -- dynamic IPs)"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}
