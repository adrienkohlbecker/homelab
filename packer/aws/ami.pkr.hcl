# AWS AMI bake for the EC2 test cells (notes/ci_aws_test_cells.md): the same
# pools.sh + chroot.sh ZFS-on-root install the qemu fixtures use, run under
# the amazon-ebssurrogate builder — no KVM anywhere. A stock Ubuntu build
# instance attaches blank EBS volumes, packer/scripts/aws_provision.sh
# resolves their Nitro NVMe device names and hands off to provision.sh, then
# packer snapshots every volume into one AMI (boot_mode=uefi, ENA). Multi-disk
# machines (pug ×3, lab ×9) ride the AMI's block device mapping, so neither
# the harness nor the launch templates carry per-machine disk logic.
#
# The `hetzner` source rides the same surrogate mechanism but ships no AMI:
# it bakes fox's Hetzner Cloud boot image (IMAGE_TARGET=hetzner) onto its one
# surrogate volume, dd+gzips that volume back off the build instance, and
# downloads it as a raw.gz for `mise run packer:hetzner` to stream onto a
# Hetzner snapshot. ebssurrogate cannot skip AMI registration, so the bake
# leaves a byproduct AMI that mise-tasks/packer/hetzner-bake.sh deregisters
# (snapshot included) right after the build.
#
# Lives in its own directory (not beside qemu.pkr.hcl): packer loads all
# sibling *.pkr.hcl files as a single configuration, and this template shares
# variable names (ubuntu_name) with the qemu one. mise run packer:ami invokes
# it directory-targeted.
#
# The shipped cell image is byte-identical in content to the qemu target
# (image_target=qemu in variant_config): gate A proved that image boots
# unmodified on EC2 — no cloud-init, netplan's en* match catches ens5, rEFInd
# via the /EFI/BOOT/BOOTX64.EFI fallback, generic kernel (in-tree ena + nvme
# suffice on Nitro; prod parity beats linux-aws).

packer {
  required_plugins {
    amazon = {
      version = "~> 1"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "ubuntu_name" {
  type = string
}

variable "build_id" {
  type        = string
  default     = "local"
  description = "Pipeline id (or 'local') stamped on the AMI/snapshot/volume tags so pruning, budgets, and incident triage do not depend on AMI names."
}

variable "manifest_path" {
  type        = string
  default     = "packer-aws-manifest.json"
  description = "Where the manifest post-processor writes the artifact list; mise-tasks/packer/ami.sh parses the AMI id out of it."
}

variable "hetzner_image_path" {
  type        = string
  default     = "packer/artifacts/hetzner/image.raw.gz"
  description = "Local path the hetzner source's gzipped raw disk image downloads to; mise-tasks/packer/hetzner-bake.sh passes the per-release path."
}

locals {
  region = "eu-central-1"

  # Canonical's per-release AMI name patterns (owner 099720109477). noble
  # and later use the gp3 path.
  source_ami_names = {
    jammy    = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
    noble    = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
    resolute = "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-*"
  }

  # Mirror of qemu.pkr.hcl's variant_config, reshaped for EBS: sizes are
  # integer GiB (EBS minimum 1, so lab's 1.5G tank disks round up to 2) and
  # disk paths are EBS mapping names the build instance resolves via
  # aws_provision.sh. rpool_disks = how many leading disks form $DISKS
  # (the rpool); the rest are $EXTRA_DISKS for pools.sh. box_deps is not a
  # source here, same as the qemu path: it derives from box by launching
  # the box AMI and seeding deps (see the design note). minimal needs no
  # bake at all (Canonical AMI alias in terraform/aws_ci.tf).
  variant_config = {
    box = {
      rpool_disks  = 1
      disk_sizes   = [40]
      layout       = ""
      swap_size    = "4G"
      extra_pools  = ""
      image_target = "qemu"
      zfs_arc_max  = ""
    }
    pug = {
      rpool_disks  = 1
      disk_sizes   = [40, 1, 1]
      layout       = ""
      swap_size    = "8G"
      extra_pools  = "apoc"
      image_target = "qemu"
      zfs_arc_max  = ""
    }
    lab = {
      rpool_disks  = 3
      disk_sizes   = [40, 40, 40, 1, 1, 2, 2, 1, 1]
      layout       = "mirror"
      swap_size    = "8G"
      extra_pools  = "dozer tank_mouse"
      image_target = "qemu"
      zfs_arc_max  = ""
    }
    # hetzner: fox's Hetzner Cloud boot image, mirroring qemu.pkr.hcl's
    # hetzner variant. Small rpool disk — state is MB; chroot.sh's
    # hetzner_growpart.service grows it into cpx22's ~76G on first boot.
    hetzner = {
      rpool_disks  = 1
      disk_sizes   = [20]
      layout       = ""
      swap_size    = "4G"
      extra_pools  = ""
      image_target = "hetzner"
      zfs_arc_max  = "536870912"
    }
  }

  # EBS mapping device letters, assigned to disk_sizes by index. /dev/xvdf
  # onward — earlier letters are conventionally reserved for the root.
  device_letters = ["f", "g", "h", "i", "j", "k", "l", "m", "n"]
  variant_devices = {
    for name, cfg in local.variant_config :
    name => [for i, _ in cfg.disk_sizes : "/dev/xvd${local.device_letters[i]}"]
  }
  variant_disks = {
    for name, cfg in local.variant_config :
    name => join(" ", slice(local.variant_devices[name], 0, cfg.rpool_disks))
  }
  variant_extra_disks = {
    for name, cfg in local.variant_config :
    name => join(" ", slice(local.variant_devices[name], cfg.rpool_disks, length(local.variant_devices[name])))
  }

  # The operator's personal public key (group_vars/all/main.yml
  # ssh_public_keys), NOT the well-known vagrant key the qemu fixtures bake:
  # cells sit on public IPs, so the key is a real boundary alongside the
  # ci-cell security group. Matches terraform's aws_key_pair.ci_operator
  # (minimal's cloud-init path); the harness relies on the operator's ssh
  # agent for the private half.
  operator_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEFQDmZidqILmoI6o9f8KLz+0hJad+Xh4Lm5OLsYDZTa adrien.kohlbecker@gmail.com",
  ]

  common_tags = {
    role     = "ci-ami"
    ubuntu   = var.ubuntu_name
    build_id = var.build_id
  }

  # Build-time fetches ride Canonical's in-region EC2 mirror (close, fast,
  # free egress); the shipped sources.list still gets the canonical upstream
  # pair (chroot.sh writes the *_UPSTREAM values into the final image — the
  # lab Nexus is unreachable from AWS either way). The regional mirror only
  # carries the archive suites; security stays on security.ubuntu.com, same
  # split the stock Canonical AMIs use.
  regional_archive  = "http://${local.region}.ec2.archive.ubuntu.com/ubuntu"
  upstream_archive  = "http://archive.ubuntu.com/ubuntu"
  upstream_security = "http://security.ubuntu.com/ubuntu"
}

# Three near-identical sources rather than one parameterized block: packer's
# build-level source overrides only reliably set attributes, and the per-
# machine difference here is the launch_block_device_mappings *blocks*.

source "amazon-ebssurrogate" "box" {
  region = local.region
  # Compute-optimized for the bake (the cells themselves ride the cheaper
  # t3a launch templates): the chroot install is CPU-bound on dpkg/zstd/
  # initramfs work, and 4 sustained vCPUs roughly halve it vs t3a.medium.
  # ~30 min on-demand is still pennies; bakes are rare and manual.
  instance_type = "c6a.xlarge"
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"

  temporary_key_pair_type     = "ed25519"
  associate_public_ip_address = true

  # Build inside the CI VPC: subnets by tag (any AZ), the ci-cell security
  # group for SSH ingress — bakes run from fox or the operator workstation,
  # both already in its allow list. No temporary world-open SG.
  subnet_filter {
    filters = { "tag:Name" = "homelab-ci-*" }
    random  = true
  }
  security_group_filter {
    filters = { "tag:Name" = "homelab-ci-cell" }
  }

  source_ami_filter {
    filters = {
      name                = local.source_ami_names[var.ubuntu_name]
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

  ami_name                = "homelab-ci-box-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI test fixture (box, ${var.ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true
  boot_mode               = "uefi"

  dynamic "launch_block_device_mappings" {
    for_each = local.variant_config["box"].disk_sizes
    content {
      device_name           = "/dev/xvd${local.device_letters[launch_block_device_mappings.key]}"
      volume_size           = launch_block_device_mappings.value
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  ami_root_device {
    source_device_name    = "/dev/xvdf"
    device_name           = "/dev/sda1"
    volume_size           = local.variant_config["box"].disk_sizes[0]
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags            = merge(local.common_tags, { machine = "box", Name = "homelab-ci-box-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.common_tags, { machine = "box" })
  run_tags        = merge(local.common_tags, { machine = "box", Name = "packer-homelab-ci-box" })
  run_volume_tags = merge(local.common_tags, { machine = "box" })
}

source "amazon-ebssurrogate" "pug" {
  region = local.region
  # Compute-optimized for the bake (the cells themselves ride the cheaper
  # t3a launch templates): the chroot install is CPU-bound on dpkg/zstd/
  # initramfs work, and 4 sustained vCPUs roughly halve it vs t3a.medium.
  # ~30 min on-demand is still pennies; bakes are rare and manual.
  instance_type = "c6a.xlarge"
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"

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
      name                = local.source_ami_names[var.ubuntu_name]
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

  ami_name                = "homelab-ci-pug-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI test fixture (pug, ${var.ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true
  boot_mode               = "uefi"

  dynamic "launch_block_device_mappings" {
    for_each = local.variant_config["pug"].disk_sizes
    content {
      device_name           = "/dev/xvd${local.device_letters[launch_block_device_mappings.key]}"
      volume_size           = launch_block_device_mappings.value
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  ami_root_device {
    source_device_name    = "/dev/xvdf"
    device_name           = "/dev/sda1"
    volume_size           = local.variant_config["pug"].disk_sizes[0]
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags            = merge(local.common_tags, { machine = "pug", Name = "homelab-ci-pug-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.common_tags, { machine = "pug" })
  run_tags        = merge(local.common_tags, { machine = "pug", Name = "packer-homelab-ci-pug" })
  run_volume_tags = merge(local.common_tags, { machine = "pug" })
}

source "amazon-ebssurrogate" "lab" {
  region = local.region
  # Compute-optimized for the bake (the cells themselves ride the cheaper
  # t3a launch templates): the chroot install is CPU-bound on dpkg/zstd/
  # initramfs work, and 4 sustained vCPUs roughly halve it vs t3a.medium.
  # ~30 min on-demand is still pennies; bakes are rare and manual.
  instance_type = "c6a.xlarge"
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"

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
      name                = local.source_ami_names[var.ubuntu_name]
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

  ami_name                = "homelab-ci-lab-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "homelab CI test fixture (lab, ${var.ubuntu_name})"
  ami_virtualization_type = "hvm"
  ena_support             = true
  boot_mode               = "uefi"

  dynamic "launch_block_device_mappings" {
    for_each = local.variant_config["lab"].disk_sizes
    content {
      device_name           = "/dev/xvd${local.device_letters[launch_block_device_mappings.key]}"
      volume_size           = launch_block_device_mappings.value
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  ami_root_device {
    source_device_name    = "/dev/xvdf"
    device_name           = "/dev/sda1"
    volume_size           = local.variant_config["lab"].disk_sizes[0]
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags            = merge(local.common_tags, { machine = "lab", Name = "homelab-ci-lab-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.common_tags, { machine = "lab" })
  run_tags        = merge(local.common_tags, { machine = "lab", Name = "packer-homelab-ci-lab" })
  run_volume_tags = merge(local.common_tags, { machine = "lab" })
}

# The fox boot image: same surrogate flow, but the artifact is the raw disk
# downloaded by the build-block provisioners below, not an AMI — the builder
# offers no skip for AMI registration, so hetzner-bake.sh deregisters the
# byproduct right after the build. The bake instance and volume choices
# mirror box's.
source "amazon-ebssurrogate" "hetzner" {
  region = local.region
  # Compute-optimized for the bake (the chroot install is CPU-bound on
  # dpkg/zstd/initramfs work), same reasoning as the cell bakes.
  instance_type = "c6a.xlarge"
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"

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
      name                = local.source_ami_names[var.ubuntu_name]
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

  ami_name                = "homelab-hetzner-${var.ubuntu_name}-{{timestamp}}"
  ami_description         = "fox Hetzner Cloud boot image (${var.ubuntu_name}) — bake byproduct, deregistered after download"
  ami_virtualization_type = "hvm"
  ena_support             = true
  boot_mode               = "uefi"

  dynamic "launch_block_device_mappings" {
    for_each = local.variant_config["hetzner"].disk_sizes
    content {
      device_name           = "/dev/xvd${local.device_letters[launch_block_device_mappings.key]}"
      volume_size           = launch_block_device_mappings.value
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  ami_root_device {
    source_device_name    = "/dev/xvdf"
    device_name           = "/dev/sda1"
    volume_size           = local.variant_config["hetzner"].disk_sizes[0]
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags            = merge(local.common_tags, { machine = "hetzner", Name = "homelab-hetzner-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.common_tags, { machine = "hetzner" })
  run_tags        = merge(local.common_tags, { machine = "hetzner", Name = "packer-homelab-hetzner" })
  run_volume_tags = merge(local.common_tags, { machine = "hetzner" })
}

build {
  sources = [
    "source.amazon-ebssurrogate.box",
    "source.amazon-ebssurrogate.pug",
    "source.amazon-ebssurrogate.lab",
    "source.amazon-ebssurrogate.hetzner",
  ]

  provisioner "file" {
    source      = "${path.root}/../scripts/"
    destination = "/home/ubuntu/"
  }

  provisioner "shell" {
    # Resolute ships sudo-rs as the default `sudo` alternative (priority 50 vs
    # classic sudo's 40). sudo-rs ignores `-E`, which strips the env block
    # below (DISKS dies as unbound in aws_provision.sh). Switch the
    # alternative back to classic sudo on resolute only, same as the qemu
    # bake; jammy/noble ship classic sudo as the default already.
    inline = concat(
      var.ubuntu_name == "resolute" ? ["sudo update-alternatives --set sudo /usr/bin/sudo.ws"] : [],
      [
        "chmod +x /home/ubuntu/*.sh",
        "sudo -HE /home/ubuntu/aws_provision.sh",
      ],
    )
    env = {
      "SOURCE_NAME" = "${source.name}"
      # Mapping names; aws_provision.sh resolves them to /dev/nvme*n1.
      "DISKS"       = local.variant_disks[source.name]
      "EXTRA_DISKS" = local.variant_extra_disks[source.name]
      "LAYOUT"      = local.variant_config[source.name].layout
      "SWAP_SIZE"   = local.variant_config[source.name].swap_size
      "EXTRA_POOLS" = local.variant_config[source.name].extra_pools
      "UBUNTU_NAME" = "${var.ubuntu_name}"
      # In-region mirror at build time, upstream in the shipped sources.list
      # (the *_UPSTREAM pair) — see the regional_archive comment above.
      "UBUNTU_MIRROR"                   = local.regional_archive
      "UBUNTU_MIRROR_SECURITY"          = local.upstream_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.upstream_security
      # Unused on the hetzner target: chroot.sh bakes no login user there
      # (cloud-init creates fox's user on first boot).
      "SSH_KEY_PUB"  = join("\n", local.operator_ssh_keys)
      "IMAGE_TARGET" = local.variant_config[source.name].image_target
      "ZFS_ARC_MAX"  = local.variant_config[source.name].zfs_arc_max
    }
  }

  # hetzner only: pull the finished image off the quiesced surrogate volume
  # (provision.sh exported the pools as its last step). aws_provision.sh
  # leaves the resolved NVMe device list in resolved_disks — the mapping
  # name /dev/xvdf doesn't exist on Nitro. gzip keeps the mostly-empty 20G
  # transfer small; packer:hetzner streams the .gz as-is.
  provisioner "shell" {
    only           = ["amazon-ebssurrogate.hetzner"]
    inline_shebang = "/bin/bash -e"
    inline = [
      "set -euo pipefail",
      "disk=\"$(cut -d' ' -f1 /home/ubuntu/resolved_disks)\"",
      "sudo dd if=\"$disk\" bs=64M | gzip >/home/ubuntu/hetzner.raw.gz",
      "ls -lh /home/ubuntu/hetzner.raw.gz",
    ]
  }

  provisioner "file" {
    only        = ["amazon-ebssurrogate.hetzner"]
    direction   = "download"
    source      = "/home/ubuntu/hetzner.raw.gz"
    destination = var.hetzner_image_path
  }

  post-processor "manifest" {
    output = var.manifest_path
  }
}
