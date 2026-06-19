# AWS AMI bake for the EC2 test cells (notes/ci_aws_test_cells.md): the same
# pools.sh + chroot.sh ZFS-on-root install the qemu fixtures use, run under
# the amazon-ebssurrogate builder — no KVM anywhere. A stock Ubuntu build
# instance attaches blank EBS volumes, packer/scripts/aws_provision.sh
# resolves their Nitro NVMe device names and hands off to provision.sh, then
# packer snapshots every volume into one AMI (boot_mode=uefi, ENA). Multi-disk
# machines (box ×2) ride the AMI's block device mapping, so neither the harness
# nor the launch templates carry per-machine disk logic.
#
# The `hetzner` source rides the same surrogate mechanism but ships no AMI:
# it bakes fox's Hetzner Cloud boot image (IMAGE_TARGET=hetzner) onto its one
# surrogate volume, then dd+gzips that volume straight off the build instance
# and onto a waiting Hetzner rescue server's /dev/sda (no image touches the
# runner). mise-tasks/packer/hetzner-bake.sh stands the rescue server up
# first and snapshots it after. ebssurrogate cannot skip AMI registration, so
# the bake leaves a byproduct AMI that hetzner-bake.sh deregisters (snapshot
# included) right after the build.
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
    # Pinned to 1.8.0, not floated, on purpose. 1.8.1 (2026-05-25) bumped its
    # vendored x/crypto to v0.52.0, whose CVE-2026-39830 fix added a "drain"
    # loop to ssh (*channel).SendRequest. In the SDK keepalive goroutine that
    # loop busy-spins a whole core per build while box_deps sits idle on
    # packer's channel during the ansible provisioner (use_proxy=false), which
    # saturated fox when concurrent bakes ran. 1.8.0 vendors x/crypto v0.43.0
    # (plain blocking SendRequest, no spin). The CVE is irrelevant here — we
    # own the build instance. Revisit once upstream fixes the busy-loop.
    amazon = {
      version = "1.8.0"
      source  = "github.com/hashicorp/amazon"
    }
    # box_deps seed converge (the amazon-ebs build below).
    ansible = {
      version = "~> 1"
      source  = "github.com/hashicorp/ansible"
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

variable "box_deps_source_ami" {
  type        = string
  default     = ""
  description = "AMI id the box_deps derivation layers onto. mise-tasks/packer/ami.sh resolves it from the /homelab-ci/ami/box/<ubuntu> SSM parameter so the seed always starts from the promoted box, never a most-recent candidate."
}

variable "cell_ssh_key" {
  type        = string
  default     = ""
  description = "Path to a private key authorized on the box AMI (the homelab-ci-cell identity in operator_ssh_keys). Empty = the local ssh agent supplies the operator key. CI passes the CI_CELL_SSH_KEY file variable."
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
  # disks form $DISKS
  # (the rpool); the rest are $EXTRA_DISKS for pools.sh. box_deps is not a
  # variant here: it derives from the promoted box AMI under the amazon-ebs
  # source below, same as the qemu path derives box_deps from box. minimal
  # needs no bake at all (Canonical AMI alias in terraform/aws_ci.tf).
  variant_config = {
    # box: 40G rpool + a 1G flat `zee` pool (second disk -> /dev/xvdg ->
    # $EXTRA_DISKS via aws_provision.sh). The second pool gives the default
    # cell multi-pool coverage (zfs trim-timer + mount-cache loops), folding
    # in what the dropped lab/pug AMIs carried. See
    # notes/ci_box_multidisk_drop_lab_pug_amis.md.
    box = {
      rpool_disks  = 1
      disk_sizes   = [40, 1]
      layout       = ""
      swap_size    = "4G"
      extra_pools  = "zee"
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
  #
  # homelab-ci-cell is the dedicated CI identity: GitLab jobs that SSH into
  # cells (today the box_deps seed bake, later the role-test matrix) get its
  # private half through the CI_CELL_SSH_KEY protected file variable —
  # never the operator's personal key.
  operator_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEFQDmZidqILmoI6o9f8KLz+0hJad+Xh4Lm5OLsYDZTa adrien.kohlbecker@gmail.com",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAIGpIVsLZs1aYR0J1Tppi8AoRMhCFjN6eGQAXG4DbsQ homelab-ci-cell",
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

# box is the sole cell-AMI surrogate source (hetzner below is a separate target
# that ships no AMI). It carries its own launch_block_device_mappings block:
# packer's build-level source overrides only reliably set scalar attributes,
# not nested blocks, so a per-machine difference there can't be parameterized.

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

# box_deps derives from the promoted box AMI: boot it, converge
# packer/seed_deps.yml over SSH (the same playbook the qemu derivation runs
# via test/launch.py --seed), stop, create-image. amazon-ebs rather than
# another ebssurrogate source because the seed needs a *booted* box, not an
# attached volume: the play reboots into the HWE kernel mid-run so podman
# bakes native-overlayfs storage config, and the podman/nginx roles start
# real services. No temporary keypair — the image has no cloud-init to
# install one, so SSH rides a key already baked into the box AMI
# (operator_ssh_keys: the operator's via agent locally, homelab-ci-cell via
# var.cell_ssh_key in CI).
source "amazon-ebs" "box_deps" {
  region = local.region
  # The cell launch template's box_deps type (terraform
  # ci_machine_instance_types): the seed itself is apt-bound and would fit
  # smaller, but reusing the cell shape keeps one less number to reconcile.
  instance_type = "m6a.large"
  ssh_username  = "vagrant"
  ssh_interface = "public_ip"

  ssh_agent_auth       = var.cell_ssh_key == ""
  ssh_private_key_file = var.cell_ssh_key != "" ? var.cell_ssh_key : null

  associate_public_ip_address = true

  subnet_filter {
    filters = { "tag:Name" = "homelab-ci-*" }
    random  = true
  }
  security_group_filter {
    filters = { "tag:Name" = "homelab-ci-cell" }
  }

  source_ami = var.box_deps_source_ami

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # Stop + create-image: boot mode, ENA, and the block-device mapping all
  # inherit from the box parent.
  ami_name = "homelab-ci-box_deps-${var.ubuntu_name}-{{timestamp}}"
  # ASCII only: EC2 rejects anything beyond it in AMI descriptions.
  ami_description = "homelab CI test fixture (box_deps, ${var.ubuntu_name}): box + pre-baked podman/nginx"

  tags            = merge(local.common_tags, { machine = "box_deps", Name = "homelab-ci-box_deps-${var.ubuntu_name}" })
  snapshot_tags   = merge(local.common_tags, { machine = "box_deps" })
  run_tags        = merge(local.common_tags, { machine = "box_deps", Name = "packer-homelab-ci-box_deps" })
  run_volume_tags = merge(local.common_tags, { machine = "box_deps" })
}

# The fox boot image: same surrogate flow, but there is no AMI artifact to
# keep. The build-block provisioner below streams the finished surrogate
# volume straight onto a waiting Hetzner rescue server (dd | gzip | ssh), so
# no 20G image ever lands on the runner. The builder offers no skip for AMI
# registration, so hetzner-bake.sh deregisters the byproduct right after the
# build. The bake instance and volume choices mirror box's.
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
  sources = [
    "source.amazon-ebssurrogate.box",
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

  # hetzner only: the ephemeral private key hetzner-bake.sh registered on the
  # rescue server, so the build instance can ssh there for the stream below.
  provisioner "file" {
    only        = ["amazon-ebssurrogate.hetzner"]
    source      = var.hetzner_rescue_key
    destination = "/home/ubuntu/rescue_key"
  }

  # hetzner only: stream the finished image straight onto the rescue server's
  # /dev/sda — no file ever lands on the runner. provision.sh exported the
  # pools as its last step, so the volume is quiesced; aws_provision.sh left
  # the resolved NVMe device in resolved_disks (the mapping name /dev/xvdf
  # doesn't exist on Nitro). zstd -T0 (all 4 vCPUs) compresses far faster than
  # gzip and matches the rpool's own zstd; -1 since the payload is already-
  # compressed blocks + zeros (speed over ratio). mbuffer on each end absorbs
  # network jitter so neither dd stalls. The rescue-side pipeline mirrors
  # RESCUE_RECV in mise-tasks/packer/_hetzner_rescue.sh — keep the two in sync.
  provisioner "shell" {
    only           = ["amazon-ebssurrogate.hetzner"]
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

  # box only: ami.sh parses the AMI id out of this manifest to promote it. The
  # hetzner source is excluded — its byproduct AMI is never promoted
  # (the deliverable is the Hetzner snapshot rescue_snapshot makes, and the
  # EXIT trap's name sweep deregisters the AMI), so it needs no manifest.
  # Skipping it also leaves the hetzner build with no post-processor phase,
  # which is where packer's manifest plugin panicked ("ConfigSpec failed:
  # connection is shut down") after an otherwise-complete stream.
  post-processor "manifest" {
    except = ["amazon-ebssurrogate.hetzner"]
    output = var.manifest_path
  }
}

# box_deps: its own build block — the surrogate build's file/shell
# provisioners (aws_provision.sh) install an OS onto blank volumes, while
# this one converges a playbook inside an already-running box.
build {
  sources = ["source.amazon-ebs.box_deps"]

  # SSH up != boot complete — the same settle gate the qemu seed path
  # (test/launch.py --seed) applies before converging. Bounded; non-running
  # final states (degraded, maintenance) fail the bake.
  provisioner "shell" {
    inline = ["timeout 300 systemctl is-system-running --wait"]
  }

  provisioner "ansible" {
    playbook_file = "${path.root}/../seed_deps.yml"
    user          = "vagrant"
    # Direct connection: ansible talks to the cell's public IP itself, so
    # the play's mid-run reboot (HWE kernel on jammy) reconnects exactly as
    # it does under the test harness; packer's communicator sits idle.
    use_proxy = false
    # Reproduce the harness inventory semantics: host `box` in group `test`
    # (host_vars/box.yml fixture vars + the test vault), with the generated
    # inventory placed at the repo root so ansible's inventory-adjacent
    # group_vars/host_vars lookup resolves — packer/seed_deps.yml has no
    # vars siblings of its own.
    host_alias          = "box"
    groups              = ["test"]
    inventory_directory = path.cwd
    # CI checkouts are world-writable, which makes ansible skip ansible.cfg
    # discovery from the cwd; pin it so the repo config (mitogen strategy,
    # vault ids, host_key_checking=False) applies. ami.sh repairs the
    # .ansible-mitogen-strategy symlink the cfg points at before building.
    # Role search is playbook-dir-relative (packer/roles), so the repo's
    # roles/ tree needs pinning too.
    ansible_env_vars = [
      "ANSIBLE_CONFIG=${path.cwd}/ansible.cfg",
      "ANSIBLE_ROLES_PATH=${path.cwd}/roles",
    ]
    extra_arguments = concat(
      # Clear nexus_url so the mirror_* Jinja in group_vars resolves to
      # upstream URLs — the lab Nexus is unreachable from AWS (same switch
      # the harness's --upstream-mirrors path passes).
      ["-e", "nexus_url="],
      # With agent auth packer has no key file to hand ansible; with an
      # explicit key, pass it through so the direct connection uses it too.
      var.cell_ssh_key != "" ? ["-e", "ansible_ssh_private_key_file=${var.cell_ssh_key}"] : [],
    )
  }

  post-processor "manifest" {
    output = var.manifest_path
  }
}
