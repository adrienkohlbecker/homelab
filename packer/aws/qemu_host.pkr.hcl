# Runner-host AMI for the nested-qemu GitLab instance executor. Unlike the
# direct EC2 cell AMIs in ami.pkr.hcl, this is a stock Ubuntu host that runs
# GitLab shell jobs and launches qemu/KVM guests from S3-hydrated image bundles.

variable "gitlab_runner_url" {
  type        = string
  default     = ""
  description = "Pinned gitlab-runner binary URL, passed by mise-tasks/packer/qemu-host-ami.sh from group_vars/all/versions.yml."
}

variable "gitlab_runner_sha256" {
  type        = string
  default     = ""
  description = "Pinned gitlab-runner binary checksum, passed by mise-tasks/packer/qemu-host-ami.sh from group_vars/all/versions.yml."
}

variable "qemu_host_build_id" {
  type        = string
  default     = "local"
  description = "Pipeline id (or 'local') stamped on the qemu-host AMI/snapshot/volume tags."
}

variable "qemu_host_manifest_path" {
  type        = string
  default     = "packer-qemu-host-manifest.json"
  description = "Where the manifest post-processor writes the qemu-host AMI artifact list."
}

locals {
  qemu_host_region          = "eu-central-1"
  qemu_host_ubuntu_name     = "noble"
  qemu_host_source_ami_name = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
  qemu_host_common_tags = {
    role     = "ci-ami"
    ubuntu   = local.qemu_host_ubuntu_name
    build_id = var.qemu_host_build_id
  }
}

source "amazon-ebs" "qemu_host" {
  region                      = local.qemu_host_region
  instance_type               = "c6a.xlarge"
  ssh_username                = "ubuntu"
  ssh_interface               = "public_ip"
  temporary_key_pair_type     = "ed25519"
  associate_public_ip_address = true

  subnet_filter {
    filters = { "tag:Name" = "homelab-ci-*" }
    random  = true
  }
  security_group_filter {
    filters = { "tag:Name" = "homelab-ci-cell" }
  }

  source_ami_filter {
    filters = {
      name                = local.qemu_host_source_ami_name
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"]
    most_recent = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 40
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  ami_name                = "homelab-ci-qemu-host-${local.qemu_host_ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI nested-qemu runner host (${local.qemu_host_ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true

  tags            = merge(local.qemu_host_common_tags, { machine = "qemu_host", Name = "homelab-ci-qemu-host-${local.qemu_host_ubuntu_name}" })
  snapshot_tags   = merge(local.qemu_host_common_tags, { machine = "qemu_host" })
  run_tags        = merge(local.qemu_host_common_tags, { machine = "qemu_host", Name = "packer-homelab-ci-qemu-host" })
  run_volume_tags = merge(local.qemu_host_common_tags, { machine = "qemu_host" })
}

build {
  sources = ["source.amazon-ebs.qemu_host"]

  provisioner "file" {
    source      = "${path.cwd}/mise.toml"
    destination = "/tmp/mise.toml"
  }

  provisioner "file" {
    source      = "${path.cwd}/pyproject.toml"
    destination = "/tmp/pyproject.toml"
  }

  provisioner "file" {
    source      = "${path.cwd}/uv.lock"
    destination = "/tmp/uv.lock"
  }

  provisioner "file" {
    source      = "${path.cwd}/mise-tasks/ci/hydrate-qemu-images.py"
    destination = "/tmp/homelab_ci_hydrate_images"
  }

  provisioner "shell" {
    inline_shebang = "/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "[ -n '${var.gitlab_runner_url}' ] || { echo 'gitlab_runner_url is required' >&2; exit 1; }",
      "[ -n '${var.gitlab_runner_sha256}' ] || { echo 'gitlab_runner_sha256 is required' >&2; exit 1; }",
      "sudo install -dm 755 /etc/apt/keyrings",
      "sudo apt-get update -qq",
      "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates curl git jq xz-utils unzip gpg gpg-agent apt-transport-https qemu-system-x86 qemu-utils ovmf openssh-client netcat-openbsd passt xorriso cloud-image-utils python3-yaml build-essential zstd tar mdadm ec2-instance-connect",
      "curl -fsSL https://mise.jdx.dev/gpg-key.pub | gpg --dearmor | sudo tee /etc/apt/keyrings/mise-archive-keyring.gpg >/dev/null",
      "echo 'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main' | sudo tee /etc/apt/sources.list.d/mise.list >/dev/null",
      "sudo apt-get update -qq",
      "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mise",
      "curl -fsSL -o /tmp/gitlab-runner '${var.gitlab_runner_url}'",
      "echo '${var.gitlab_runner_sha256}  /tmp/gitlab-runner' | sha256sum -c -",
      "sudo install -m 0755 -o root -g root /tmp/gitlab-runner /usr/local/bin/gitlab-runner",
      "sudo ln -sf /usr/local/bin/gitlab-runner /usr/bin/gitlab-runner",
      "sudo install -m 0755 -o root -g root /tmp/homelab_ci_hydrate_images /usr/local/bin/homelab_ci_hydrate_images",
      "sudo usermod -aG kvm ubuntu",
      "sudo install -dm 0755 -o ubuntu -g ubuntu /mnt/scratch/homelab_ci",
      "sudo install -dm 0755 /opt/mise /opt/uv-cache /opt/venv /etc/mise /tmp/homelab-ci-build",
      "sudo mv /tmp/mise.toml /tmp/pyproject.toml /tmp/uv.lock /tmp/homelab-ci-build/",
      "cd /tmp/homelab-ci-build",
      "sudo env MISE_DATA_DIR=/opt/mise PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin mise trust /tmp/homelab-ci-build/mise.toml",
      "sudo env MISE_DATA_DIR=/opt/mise PATH=/opt/mise/shims:/usr/local/bin:/usr/bin:/bin mise install",
      "sudo env MISE_DATA_DIR=/opt/mise UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv MISE_PYTHON_UV_VENV_AUTO=false PATH=/opt/venv/bin:/opt/mise/shims:/usr/local/bin:/usr/bin:/bin UV_COMPILE_BYTECODE=1 mise exec -- uv sync --frozen --link-mode hardlink",
      "sudo awk '/^\\[tools\\]/{p=1; print; next} /^\\[/{p=0} p' /tmp/homelab-ci-build/mise.toml | sudo tee /etc/mise/config.toml >/dev/null",
      "sudo chown -R ubuntu:ubuntu /opt/mise /opt/uv-cache /opt/venv",
      "sudo tee /usr/local/bin/homelab_ci_prepare_scratch >/dev/null <<'EOF'\n#!/usr/bin/env bash\nset -euo pipefail\ninstall -dm 0755 -o ubuntu -g ubuntu /mnt/scratch/homelab_ci\nchown ubuntu:ubuntu /mnt/scratch/homelab_ci\nEOF",
      "sudo chmod 0755 /usr/local/bin/homelab_ci_prepare_scratch",
      "sudo tee /usr/local/bin/homelab_ci_ready >/dev/null <<'EOF'\n#!/usr/bin/env bash\nset -euo pipefail\n[ -c /dev/kvm ]\n[ -r /dev/kvm ]\n[ -w /dev/kvm ]\n[ -w /mnt/scratch/homelab_ci ]\nenv -i PATH=/usr/bin:/bin gitlab-runner --version >/dev/null\ncommand -v qemu-system-x86_64 >/dev/null\ncommand -v qemu-img >/dev/null\ncommand -v passt >/dev/null\ncommand -v mise >/dev/null\nEOF",
      "sudo chmod 0755 /usr/local/bin/homelab_ci_ready",
      "sudo tee /etc/systemd/system/homelab-ci-scratch.service >/dev/null <<'EOF'\n[Unit]\nDescription=Prepare homelab CI qemu scratch cache\nBefore=multi-user.target\n\n[Service]\nType=oneshot\nExecStart=/usr/local/bin/homelab_ci_prepare_scratch\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\nEOF",
      "sudo systemctl enable homelab-ci-scratch.service",
      "sudo apt-get clean",
      "sudo rm -rf /var/lib/apt/lists/* /tmp/gitlab-runner /tmp/homelab-ci-build",
    ]
  }

  post-processor "manifest" {
    output = var.qemu_host_manifest_path
  }
}
