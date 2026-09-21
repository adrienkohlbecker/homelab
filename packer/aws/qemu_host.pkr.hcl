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

variable "architecture" {
  type        = string
  default     = "x86_64"
  description = "Target qemu-host architecture."

  validation {
    condition     = contains(["x86_64", "aarch64"], var.architecture)
    error_message = "Architecture must be x86_64 or aarch64."
  }
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
  versions       = yamldecode(file("${path.cwd}/group_vars/all/versions.yml"))
  ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))
  ubuntu_version = local.ubuntu_catalog.releases[var.ubuntu_name].version
  # Region and AMI naming are shared with qemu-host-ami.sh, the audit, and
  # Terraform's launch templates.
  architecture_data = yamldecode(file("${path.cwd}/data/architectures.yml"))[var.architecture]
  ci_architecture   = local.architecture_data.ci
  # The image smoke test boots the harness's guest machine type.
  guest_architecture = local.architecture_data.guest

  architecture_table = {
    x86_64 = {
      ami_architecture     = "amd64"
      builder_instance     = "c6a.xlarge"
      qemu_packages        = "qemu-system-x86 ovmf"
      qemu_system_binary   = "qemu-system-x86_64"
      runner_artifact      = local.versions.gitlab_runner_archive.x86_64
      cloudwatch_agent     = local.versions.cloudwatch_agent_deb.x86_64
      firmware_destination = ""
    }
    aarch64 = {
      ami_architecture     = "arm64"
      builder_instance     = "c7g.xlarge"
      qemu_packages        = "qemu-system-arm qemu-efi-aarch64"
      qemu_system_binary   = "qemu-system-aarch64"
      runner_artifact      = local.versions.gitlab_runner_archive.aarch64
      cloudwatch_agent     = local.versions.cloudwatch_agent_deb.aarch64
      firmware_destination = "/opt/homelab-ci/qemu-firmware/aarch64"
    }
  }
  architecture_config = local.architecture_table[var.architecture]

  qemu_host_common_tags = {
    role         = "ci-ami"
    ubuntu       = var.ubuntu_name
    architecture = var.architecture
    build_id     = var.qemu_host_build_id
  }
}

source "amazon-ebs" "qemu_host" {
  region                                    = local.ci_architecture.aws_region
  instance_type                             = local.architecture_config.builder_instance
  ssh_username                              = "ubuntu"
  ssh_interface                             = "public_ip"
  temporary_key_pair_type                   = "ed25519"
  temporary_security_group_source_public_ip = true
  associate_public_ip_address               = true

  subnet_filter {
    filters = { "tag:Name" = "homelab-ci-*" }
    random  = true
  }
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-${var.ubuntu_name}-${local.ubuntu_version}-${local.architecture_config.ami_architecture}-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"]
    most_recent = true
  }

  # A CI job timeout can kill Packer before its own cleanup runs. The build
  # instance then ends itself: EC2 terminates it on an OS-initiated shutdown,
  # and user data schedules that shutdown well past the bake job's timeout.
  # The scheduled shutdown lives in /run, so the AMI does not inherit it.
  shutdown_behavior = "terminate"
  user_data         = "#!/bin/sh\nshutdown -h +180\n"

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

  ami_name                = "${local.ci_architecture.ami_name_prefix}-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI nested-qemu runner host (${var.architecture}, ${var.ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true

  # Keep machine explicit in each map so every artifact documents its
  # qemu_host value in place; the Hetzner image is a separate target.
  tags            = merge(local.qemu_host_common_tags, { machine = "qemu_host", Name = "${local.ci_architecture.ami_name_prefix}-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.qemu_host_common_tags, { machine = "qemu_host" })
  run_tags        = merge(local.qemu_host_common_tags, { machine = "qemu_host", Name = "packer-homelab-ci-qemu-host" })
  run_volume_tags = merge(local.qemu_host_common_tags, { machine = "qemu_host" })
}

build {
  sources = ["source.amazon-ebs.qemu_host"]

  # The stock AMI boots linux-aws, a rolling flavour that runs ahead of the GA
  # kernel the fleet is on. Swap it and reboot before anything else, so every
  # later provisioner -- and the smoke tests that gate the capture -- exercises
  # the kernel the image will actually boot.
  provisioner "shell" {
    script = "${path.root}/files/install_ga_kernel.sh"
  }

  provisioner "shell" {
    expect_disconnect = true
    inline            = ["sudo systemctl reboot"]
  }

  provisioner "file" {
    # Let sshd finish going down, so the upload is not raced onto the
    # connection that is about to be torn down by the reboot above.
    pause_before = "30s"

    sources = [
      "${path.cwd}/mise.toml",
      "${path.cwd}/pyproject.toml",
      "${path.cwd}/uv.lock",
      "${path.cwd}/packer/aws/files/homelab_ci_prepare_scratch.sh",
      "${path.cwd}/packer/aws/files/gitlab_runner_fleeting_arm.pub",
      "${path.cwd}/packer/aws/files/qemu_host_smoke.sh",
      "${path.cwd}/packer/aws/files/cloudwatch_agent.json",
      # The ARM firmware installer and the pins it reads.
      "${path.cwd}/mise-tasks/test/firmware.sh",
      "${path.cwd}/group_vars/all/versions.yml",
      "${path.cwd}/data/architectures.yml",
      # The boot-time image pre-hydration and the modules and data it reads.
      "${path.cwd}/mise-tasks/ci/hydrate-qemu-images.py",
      "${path.cwd}/mise-tasks/ci/qemu_image_store.py",
      "${path.cwd}/test/matrix.py",
      "${path.cwd}/data/ubuntu_releases.yml",
    ]
    destination = "/tmp/"
  }

  # The reboot above dropped the shutdown user data scheduled in /run, so
  # schedule it again to keep the build instance self-terminating.
  provisioner "shell" {
    inline = ["sudo shutdown -h +180"]
  }

  provisioner "shell" {
    script = "${path.root}/files/provision_qemu_host.sh"
    env = {
      "TARGET_ARCHITECTURE"          = var.architecture
      "QEMU_PACKAGES"                = local.architecture_config.qemu_packages
      "QEMU_SYSTEM_BINARY"           = local.architecture_config.qemu_system_binary
      "QEMU_MACHINE_TYPE"            = local.guest_architecture.machine_type
      "GITLAB_RUNNER_URL"            = local.architecture_config.runner_artifact.url
      "GITLAB_RUNNER_SHA256"         = local.architecture_config.runner_artifact.sha256
      "CLOUDWATCH_AGENT_URL"         = local.architecture_config.cloudwatch_agent.url
      "CLOUDWATCH_AGENT_SHA256"      = local.architecture_config.cloudwatch_agent.sha256
      "HOMELAB_AARCH64_FIRMWARE_DIR" = local.architecture_config.firmware_destination
    }
  }

  post-processor "manifest" {
    output = var.qemu_host_manifest_path
  }
}
