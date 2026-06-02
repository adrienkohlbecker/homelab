# Hetzner Cloud CI worker image. Builds a snapshot with all tooling
# pre-installed so an ephemeral CCX instance can boot, register as a
# GitHub Actions runner, and start running QEMU-based test cells within
# minutes. No ZFS — plain ext4 root on a stock Ubuntu 24.04 base.
#
# Build: `mise run packer:worker`
# The snapshot is labelled role=ci-worker,ubuntu=noble so terraform /
# the provisioning script can select the newest matching snapshot.

packer {
  required_plugins {
    hcloud = {
      source  = "github.com/hetznercloud/hcloud"
      version = ">= 1.7.2"
    }
  }
}

variable "hcloud_token" {
  type      = string
  sensitive = true
  default   = env("HCLOUD_TOKEN")
}

variable "location" {
  type    = string
  default = "nbg1"
}

variable "server_type" {
  type        = string
  default     = "cpx22"
  description = "Hetzner server type the build VM is created as. Provisioning does not need KVM so any shared-vCPU type works."
}

variable "firewall_ids" {
  type        = list(number)
  default     = []
  description = "Hetzner firewall IDs to attach to the build server. The mise task resolves the 'fox' firewall by name."
}

variable "mise_github_token" {
  type      = string
  sensitive = true
  default   = ""
}

# Build on a small instance (provisioning doesn't need KVM); the
# snapshot works on any server type including CCX.
source "hcloud" "worker" {
  token        = var.hcloud_token
  image        = "ubuntu-24.04"
  location     = var.location
  server_type  = var.server_type
  ssh_username = "root"

  firewall_ids  = var.firewall_ids
  server_name   = "packer-ci-worker"
  snapshot_name = "ci-worker-noble-{{timestamp}}"
  snapshot_labels = {
    role   = "ci-worker"
    ubuntu = "noble"
  }
}

build {
  sources = ["source.hcloud.worker"]

  provisioner "file" {
    source      = "${path.root}/../mise.toml"
    destination = "/tmp/mise.toml"
  }

  provisioner "file" {
    source      = "${path.root}/../pyproject.toml"
    destination = "/tmp/pyproject.toml"
  }

  provisioner "file" {
    source      = "${path.root}/../uv.lock"
    destination = "/tmp/uv.lock"
  }

  provisioner "file" {
    source      = "${path.root}/qemu.pkr.hcl"
    destination = "/tmp/qemu.pkr.hcl"
  }

  provisioner "file" {
    source      = "${path.root}/ubuntu_images.json"
    destination = "/tmp/ubuntu_images.json"
  }

  provisioner "shell" {
    script = "${path.root}/scripts/provision_worker.sh"
    environment_vars = [
      "MISE_GITHUB_TOKEN=${var.mise_github_token}",
    ]
  }
}
