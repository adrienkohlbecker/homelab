# fox's Hetzner Cloud boot image bake (notes/ci_aws_test_cells.md): the same
# pools.sh + chroot.sh ZFS-on-root install the qemu fixtures use, run under
# the amazon-ebssurrogate builder — no KVM anywhere. A stock Ubuntu build
# instance attaches a blank EBS volume, packer/scripts/aws_provision.sh
# resolves its Nitro NVMe device name and hands off to provision.sh.
#
# The `hetzner` source rides the surrogate mechanism but ships no AMI: it bakes
# fox's Hetzner Cloud boot image (IMAGE_TARGET=hetzner) onto its one surrogate
# volume, then dd+zstds that volume straight off the build instance and onto a
# waiting Hetzner rescue server's /dev/sda (no image touches the runner).
# mise-tasks/packer/hetzner-bake.sh stands the rescue server up first and
# snapshots it after. ebssurrogate cannot skip AMI registration, so the bake
# leaves a byproduct AMI that hetzner-bake.sh deregisters (snapshot included)
# right after the build.
#
# Lives in its own directory (not beside qemu.pkr.hcl): packer loads all
# sibling *.pkr.hcl files as a single configuration, and this template shares
# variable names (ubuntu_name) with the qemu one. mise run packer:hetzner-bake
# invokes it directory-targeted.

packer {
  required_plugins {
    # Pinned to 1.8.0, not floated, on purpose. 1.8.1 (2026-05-25) bumped its
    # vendored x/crypto to v0.52.0, whose CVE-2026-39830 fix added a "drain"
    # loop to ssh (*channel).SendRequest. In the SDK keepalive goroutine that
    # loop busy-spins a whole core per build, which saturated fox when
    # concurrent bakes ran. 1.8.0 vendors x/crypto v0.43.0 (plain blocking
    # SendRequest, no spin). The CVE is irrelevant here — we own the build
    # instance. Revisit once upstream fixes the busy-loop.
    amazon = {
      version = "1.8.0"
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

variable "hetzner_rescue_ip" {
  type        = string
  default     = ""
  description = "Public IP of the Hetzner rescue server the hetzner source streams its baked disk onto. mise-tasks/packer/hetzner-bake.sh sets it after creating the rescue server; empty fails the stream provisioner (the hetzner source has no other artifact)."
}

variable "hetzner_rescue_key" {
  type        = string
  default     = "/dev/null"
  description = "Path to the ephemeral private key authorized on the Hetzner rescue server, uploaded to the build instance for the direct stream. mise-tasks/packer/hetzner-bake.sh passes the key it registered; the /dev/null default only keeps `packer validate` happy (the empty rescue_ip fails the stream first)."
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
  # integer GiB (EBS minimum 1) and disk paths are EBS mapping names the build
  # instance resolves via aws_provision.sh. rpool_disks = how many leading
  # disks form $DISKS (the rpool); the rest are $EXTRA_DISKS for pools.sh.
  variant_config = {
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
  # ssh_public_keys) authorizes the bake instance's SSH. Unused on the shipped
  # hetzner image — chroot.sh bakes no login user there (cloud-init creates
  # fox's user on first boot) — but aws_provision.sh still expects SSH_KEY_PUB.
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

# The fox boot image: the surrogate flow, but there is no AMI artifact to
# keep. The build-block provisioner below streams the finished surrogate
# volume straight onto a waiting Hetzner rescue server (dd | zstd | ssh), so
# no 20G image ever lands on the runner. The builder offers no skip for AMI
# registration, so hetzner-bake.sh deregisters the byproduct right after the
# build.
source "amazon-ebssurrogate" "hetzner" {
  region = local.region
  # Compute-optimized for the bake (the chroot install is CPU-bound on
  # dpkg/zstd/initramfs work). ~30 min on-demand is still pennies; bakes are
  # rare and manual.
  instance_type = "c6a.xlarge"
  ssh_username  = "ubuntu"
  ssh_interface = "public_ip"

  temporary_key_pair_type     = "ed25519"
  associate_public_ip_address = true

  # Build inside the CI VPC: subnets by tag (any AZ), the qemu-host security
  # group for SSH ingress — bakes run from fox, already in its allow list.
  # No temporary world-open SG.
  subnet_filter {
    filters = { "tag:Name" = "homelab-ci-*" }
    random  = true
  }
  security_group_filter {
    filters = { "tag:Name" = "homelab-ci-qemu-host" }
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
  ami_description         = "fox Hetzner Cloud boot image (${var.ubuntu_name}): bake byproduct, deregistered after the stream"
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
  sources = ["source.amazon-ebssurrogate.hetzner"]

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

  # The ephemeral private key hetzner-bake.sh registered on the rescue server,
  # so the build instance can ssh there for the stream below.
  provisioner "file" {
    source      = var.hetzner_rescue_key
    destination = "/home/ubuntu/rescue_key"
  }

  # Stream the finished image straight onto the rescue server's /dev/sda — no
  # file ever lands on the runner. provision.sh exported the pools as its last
  # step, so the volume is quiesced; aws_provision.sh left the resolved NVMe
  # device in resolved_disks (the mapping name /dev/xvdf doesn't exist on
  # Nitro). zstd -T0 (all 4 vCPUs) compresses far faster than gzip and matches
  # the rpool's own zstd; -1 since the payload is already-compressed blocks +
  # zeros (speed over ratio). mbuffer on each end absorbs network jitter so
  # neither dd stalls. The rescue-side pipeline mirrors RESCUE_RECV in
  # mise-tasks/packer/_hetzner_rescue.sh — keep the two in sync.
  provisioner "shell" {
    inline_shebang = "/bin/bash -e"
    inline = [
      "set -euxo pipefail",
      "rescue_ip='${var.hetzner_rescue_ip}'",
      "[ -n \"$rescue_ip\" ] || { echo 'hetzner_rescue_ip unset — build the hetzner image via mise run packer:hetzner-bake' >&2; exit 1; }",
      "chmod 600 /home/ubuntu/rescue_key",
      "sudo apt-get update -qq && sudo apt-get install -y -qq zstd mbuffer",
      "disk=\"$(cut -d' ' -f1 /home/ubuntu/resolved_disks)\"",
      "echo \"==> streaming $disk -> root@$rescue_ip:/dev/sda\"",
      "sudo dd if=\"$disk\" bs=64M | zstd -1 -T0 | mbuffer -m 512M | ssh -i /home/ubuntu/rescue_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \"root@$rescue_ip\" 'mbuffer -q -m 512M | zstd -dc | dd of=/dev/sda bs=64M conv=sparse status=progress; sync'",
      "echo '==> stream complete'",
    ]
  }
}
