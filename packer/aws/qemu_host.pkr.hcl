# Runner-host AMI for the nested-qemu GitLab instance executor: a stock Ubuntu
# host that runs GitLab shell jobs and launches qemu/KVM guests from S3-hydrated
# image bundles.

packer {
  required_plugins {
    # Pinned rather than floated: 1.8.1's x/crypto v0.52.0 busy-spins in
    # ssh (*channel).SendRequest when the response channel is closed. 1.8.2
    # vendors v0.54.0, which detects the closed channel and exits the drain.
    amazon = {
      version = "1.8.2"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "ubuntu_name" {
  type        = string
  default     = "noble"
  description = "Ubuntu release name."
}

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
  qemu_host_common_tags = {
    role     = "ci-ami"
    ubuntu   = var.ubuntu_name
    build_id = var.qemu_host_build_id
  }
}

source "amazon-ebs" "qemu_host" {
  region                      = "eu-central-1"
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
    filters = { "tag:Name" = "homelab-ci-qemu-host" }
  }

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
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

  ami_name                = "homelab-ci-qemu-host-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI nested-qemu runner host (${var.ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true

  tags            = merge(local.qemu_host_common_tags, { machine = "qemu_host", Name = "homelab-ci-qemu-host-${var.ubuntu_name}" })
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

  provisioner "file" {
    source      = "${path.cwd}/packer/aws/files/homelab_ci_prepare_scratch.sh"
    destination = "/tmp/homelab_ci_prepare_scratch"
  }

  provisioner "shell" {
    script = "${path.root}/files/provision_qemu_host.sh"
    env = {
      "GITLAB_RUNNER_URL"    = var.gitlab_runner_url
      "GITLAB_RUNNER_SHA256" = var.gitlab_runner_sha256
    }
  }

  post-processor "manifest" {
    output = var.qemu_host_manifest_path
  }
}
