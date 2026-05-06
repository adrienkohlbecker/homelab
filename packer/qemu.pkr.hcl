packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "ubuntu_name" {
  type = string
}

variable "output_base" {
  type        = string
  description = "Staging directory packer writes into. Each source writes <output_base>/<source-name>. mise-tasks/packer/build sets this to a fresh tmpdir under QEMU_DIR/<ubuntu>/ so the previous good artifacts under artifact_base stay intact while the new ones build."
}

variable "artifact_base" {
  type        = string
  description = "Parent directory of final per-source artifact dirs. The install post-processor renames <output_base>/<source-name> -> <artifact_base>/<source-name> after verify + compress pass."
}

variable "arch" {
  type        = string
  default     = "x86_64"
  description = "Host arch for the build (x86_64 or aarch64). Auto-resolved by mise.toml's [env] PKR_VAR_arch from the host's arch()."
  validation {
    condition     = contains(["x86_64", "aarch64"], var.arch)
    error_message = "Arch must be one of: x86_64, aarch64."
  }
}

variable "upstream_mirrors" {
  type        = bool
  default     = false
  description = "When true, pull apt packages and the cloud image straight from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
}

locals {
  # Cloud image dated snapshots. Bump when refreshing; older snapshots
  # eventually fall out of the upstream listing (and out of the Nexus
  # proxy cache). Same role as the previous `ubuntu_versions.patch`
  # field for live-server ISOs.
  ubuntu_snapshots = {
    jammy    = "20260320"
    noble    = "20260323"
    resolute = "20260421"
  }
  ubuntu_snapshot = local.ubuntu_snapshots[var.ubuntu_name]

  # Arch-keyed configuration table. Centralizes everything that varies
  # between the supported builds. In this stack arch is a 1:1 proxy for
  # OS (x86_64 = Linux + KVM, aarch64 = arm Mac + HVF); if arm64 Linux
  # ever joins the mix, this needs to split by os too — accelerator,
  # image_format, and firmware paths would all diverge.
  #
  # Field notes:
  # - accelerator: kvm on Linux, hvf on Mac.
  # - efi_firmware_*: ovmf on Linux, Homebrew qemu (aarch64 code + arm
  #   vars) on Mac.
  # - image_format: raw on Linux (dozer/scratch already does CoW + zstd
  #   so qcow2 stacks redundant work); qcow2 on Mac (APFS has no
  #   fs-level compression).
  # - qemuargs: aarch64's `virt` machine ships no default graphics or
  #   input devices so VNC would be blank without these. q35 already
  #   has std VGA + PS/2 keyboard, so the x86_64 list is empty.
  # - cloud_image_suffix: filename token in the upstream cloud-image
  #   tarball naming.
  # - upstream/nexus_archive/security: x86_64 ships glibc on
  #   archive/security; aarch64 lives under ports.
  # rEFInd binary names + ZBM version live in chroot.sh (per-arch case
  # block) — the build VM and the shipped image are always the same
  # arch, so chroot.sh can derive them from `uname -m` itself.
  arch_table = {
    x86_64 = {
      machine_type       = "q35"
      accelerator        = "kvm"
      efi_firmware_code  = "/usr/share/OVMF/OVMF_CODE.fd"
      efi_firmware_vars  = "/usr/share/OVMF/OVMF_VARS.fd"
      image_format       = "raw"
      qemuargs           = []
      cloud_image_suffix = "amd64"
      upstream_archive   = "http://archive.ubuntu.com/ubuntu"
      upstream_security  = "http://security.ubuntu.com/ubuntu"
      nexus_archive      = "http://nexus.lab.fahm.fr/repository/ubuntu-archive"
      nexus_security     = "http://nexus.lab.fahm.fr/repository/ubuntu-security"
    }
    aarch64 = {
      machine_type      = "virt"
      accelerator       = "hvf"
      efi_firmware_code = "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
      efi_firmware_vars = "/opt/homebrew/share/qemu/edk2-arm-vars.fd"
      image_format      = "qcow2"
      qemuargs = [
        ["-device", "virtio-gpu-pci"],
        ["-device", "qemu-xhci"],
        ["-device", "usb-kbd"],
        ["-device", "usb-tablet"],
      ]
      cloud_image_suffix = "arm64"
      upstream_archive   = "http://ports.ubuntu.com/ubuntu-ports"
      upstream_security  = "http://ports.ubuntu.com/ubuntu-ports"
      nexus_archive      = "http://nexus.lab.fahm.fr/repository/ubuntu-ports"
      nexus_security     = "http://nexus.lab.fahm.fr/repository/ubuntu-ports"
    }
  }
  arch_cfg = local.arch_table[var.arch]

  # Per-source config consumed downstream:
  # - machine: the test machine spec verify-boot drives.
  # - disks: space-delimited list of whole-disk paths the build VM
  #   exposes to provision.sh as $DISKS. The qemu source declares the
  #   disks in disk_additional_size; this string mirrors what they end
  #   up as inside the guest.
  # - layout: zpool create layout token — "" for single-disk, "mirror"
  #   for an rpool mirror. Consumed by provision.sh and chroot.sh.
  # Add an entry whenever a new source "qemu.ubuntu" block joins the build.
  variant_config = {
    zfs = {
      machine = "box"
      disks   = "/dev/vdb"
      layout  = ""
    }
    zfs-lab = {
      machine = "lab"
      disks   = "/dev/vdb /dev/vdc /dev/vdd"
      layout  = "mirror"
    }
  }

  # Search PATH for qemu-system-{arch}; lets the same template work on
  # Linux (`/usr/bin/qemu-system-x86_64`) and Mac (`/opt/homebrew/bin/...`)
  # without per-OS path hacks.
  qemu_binary = "qemu-system-${var.arch}"

  # Single source of truth for the vagrant authorized keys. Rendered
  # into both the cloud-init seed (for the build VM) and chroot.sh's
  # vagrant authorized_keys (for the shipped install).
  vagrant_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y vagrant insecure public key",
  ]

  # Cloud image base URLs. Defaults to the Nexus proxy
  # (`terraform/nexus.tf` raw_proxies "ubuntu-cloud-images"); set
  # `-var upstream_mirrors=true` to bypass it.
  upstream_cloud_base = "https://cloud-images.ubuntu.com/${var.ubuntu_name}/${local.ubuntu_snapshot}"
  nexus_cloud_base    = "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images/${var.ubuntu_name}/${local.ubuntu_snapshot}"
  cloud_base          = var.upstream_mirrors ? local.upstream_cloud_base : local.nexus_cloud_base
  cloud_checksum      = "file:${local.cloud_base}/SHA256SUMS"
  cloud_url           = "${local.cloud_base}/${var.ubuntu_name}-server-cloudimg-${local.arch_cfg.cloud_image_suffix}.img"

  # Apt mirrors. By default the build pulls through the lab Nexus proxy
  # (`group_vars/all.yml` uses the same `repository/ubuntu-*` layout); set
  # `-var upstream_mirrors=true` to bypass it. The `upstream_*` pair is
  # always the canonical Ubuntu URL — chroot.sh writes those into the
  # final `/etc/apt/sources.list` so we don't ship Nexus-internal URLs.
  build_archive  = var.upstream_mirrors ? local.arch_cfg.upstream_archive : local.arch_cfg.nexus_archive
  build_security = var.upstream_mirrors ? local.arch_cfg.upstream_security : local.arch_cfg.nexus_security
}

source "qemu" "ubuntu" {
  accelerator        = "${local.arch_cfg.accelerator}"
  boot_wait          = "2s"
  cpu_model          = "host"
  cores              = 8
  sockets            = 1
  disk_cache         = "unsafe"
  disk_compression   = false
  disk_detect_zeroes = "unmap"
  disk_discard       = "unmap"
  disk_image         = true
  disk_interface     = "virtio"
  disk_size          = "10G"
  efi_boot           = true
  efi_firmware_code  = "${local.arch_cfg.efi_firmware_code}"
  efi_firmware_vars  = "${local.arch_cfg.efi_firmware_vars}"
  format             = "${local.arch_cfg.image_format}"
  headless           = true
  iso_checksum       = "${local.cloud_checksum}"
  iso_url            = "${local.cloud_url}"
  # NoCloud datasource: cloud-init auto-detects an attached CD/ISO
  # labelled `cidata` containing user-data + meta-data. cd_content
  # renders these inline via templatefile() so the vagrant pubkey and
  # build-time apt mirror URLs land in the seed without an on-disk
  # template file. meta-data is empty but the file must exist.
  cd_label = "cidata"
  cd_content = {
    "user-data" = templatefile("http/user-data.pkrtpl", {
      archive_url  = local.build_archive
      security_url = local.build_security
      ssh_keys     = local.vagrant_ssh_keys
    })
    "meta-data" = ""
  }
  machine_type         = "${local.arch_cfg.machine_type}"
  memory               = 4096
  net_device           = "virtio-net"
  qemu_binary          = "${local.qemu_binary}"
  shutdown_command     = "sudo /usr/sbin/shutdown -h now"
  skip_compaction      = true
  ssh_private_key_file = "${path.root}/vagrant.key"
  ssh_timeout          = "20m"
  ssh_username         = "vagrant"
  # Local-only VNC. To watch a build from another host, tunnel:
  #   ssh -L 5900:127.0.0.1:5900 <build-host>
  # then connect a VNC client to localhost:5900.
  vnc_bind_address = "127.0.0.1"
  qemuargs = concat([
    ["-object", "rng-random,id=rng0,filename=/dev/urandom"],
    ["-device", "virtio-rng-pci,rng=rng0"],
  ], local.arch_cfg.qemuargs)

  # QMP socket lands at <output_dir>/qmp.sock and lets the build be poked
  # out-of-band: `echo '{"execute":"qmp_capabilities"}{"execute":"system_reset"}' \
  #   | socat - UNIX-CONNECT:<output_dir>/qmp.sock` resets the guest without
  # killing qemu, which is much faster than re-running packer when iterating
  # on bootloader/firmware bits like ZBM. Other useful commands: system_powerdown
  # (ACPI shutdown), stop / cont, screendump.
  qmp_enable = true
}

build {

  # zfs: single-disk rpool. Consumed by the box and pug test
  # variants. Per-variant differences (extra pools like apoc) are created
  # at test boot via test/disks/<variant>.sh, not baked here.
  source "qemu.ubuntu" {
    name                 = "zfs"
    output_directory     = "${var.output_base}/zfs"
    disk_additional_size = ["40G"]
    host_port_max        = 2231
    host_port_min        = 2222
    vnc_port_max         = 5909
    vnc_port_min         = 5900
  }

  # zfs-lab: mdadm-EFI + mdadm-swap + 3-disk mirror rpool. Consumed
  # by the lab test variant; matches the lab-class prod host shape. Also
  # serves as the multi-disk regression for provision.sh / chroot.sh and
  # as a copy-paste reference for provisioning new lab-class prod hosts.
  # See AGENTS.md "Test Environment Design".
  source "qemu.ubuntu" {
    name                 = "zfs-lab"
    output_directory     = "${var.output_base}/zfs-lab"
    disk_additional_size = ["40G", "40G", "40G"]
    host_port_max        = 2241
    host_port_min        = 2232
    vnc_port_max         = 5919
    vnc_port_min         = 5910
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    # Resolute ships sudo-rs as the default `sudo` alternative (priority 50 vs
    # classic sudo's 40). sudo-rs silently ignores the SETENV sudoers tag, so
    # `sudo -E` strips the env block below. Switch the alternative back to
    # classic sudo (which honors SETENV + -E) on resolute only; jammy/noble
    # ship classic sudo as the default already.
    inline = concat(
      var.ubuntu_name == "resolute" ? ["sudo update-alternatives --set sudo /usr/bin/sudo.ws"] : [],
      ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"],
    )
    # Mirror URLs are resolved here (HCL) and passed as env. provision.sh
    # uses UBUNTU_MIRROR* during the build; chroot.sh swaps in the
    # UBUNTU_MIRROR_*_UPSTREAM pair at the end so the shipped image
    # never points at Nexus.
    env = {
      "SOURCE_NAME"                     = "${source.name}"
      "DISKS"                           = local.variant_config[source.name].disks
      "LAYOUT"                          = local.variant_config[source.name].layout
      "UBUNTU_NAME"                     = "${var.ubuntu_name}"
      "UBUNTU_MIRROR"                   = local.build_archive
      "UBUNTU_MIRROR_SECURITY"          = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.arch_cfg.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.arch_cfg.upstream_security
      "SSH_KEY_PUB"                     = join("\n", local.vagrant_ssh_keys)
    }
  }

  # Pull the kernel/initrd/cmdline that provision.sh staged on the
  # build VM down into the artifacts directory. The test harness
  # consumes these on arches where the rEFInd -> ZBM -> kexec chain
  # panics on EDK2 (aarch64) and direct-boots via -kernel/-initrd.
  # Three explicit provisioners (one per file) because packer's
  # download direction always preserves the source's leaf directory
  # regardless of trailing slash.
  provisioner "file" {
    direction   = "download"
    source      = "/home/vagrant/extracted/kernel"
    destination = "${var.output_base}/${source.name}/kernel"
  }

  provisioner "file" {
    direction   = "download"
    source      = "/home/vagrant/extracted/initrd"
    destination = "${var.output_base}/${source.name}/initrd"
  }

  provisioner "file" {
    direction   = "download"
    source      = "/home/vagrant/extracted/cmdline"
    destination = "${var.output_base}/${source.name}/cmdline"
  }

  # Sequential chain: drop the cloud-image disk, smoke-test the boot,
  # compress (Mac only). All three side-effect-only on ${var.output_base}/${source.name}.
  # The final .new -> artdir rename is owned by mise-tasks/packer/build.
  post-processors {
    # packer-ubuntu is the residual cloud-image OS disk — provision.sh
    # debootstraps onto packer-ubuntu-1..N and never writes to vda, so
    # nothing downstream consumes it.
    post-processor "shell-local" {
      name           = "drop-cloudimg-disk"
      inline_shebang = "/bin/bash"
      inline = [
        "set -euxo pipefail",
        "rm -f ${var.output_base}/${source.name}/packer-ubuntu",
      ]
    }

    # Boot the freshly-built image and wait for systemd-fully-booted.
    # Runs before compress so a failed verify short-circuits without
    # burning CPU on a dead image. test/launch.py with --exit-after-ready
    # boots the variant, waits for SSH, runs `systemctl is-system-running
    # --wait`, and exits 0 only on state "running". The 200-line tail on
    # failure spares the operator a reproduction run for trivial breakage.
    post-processor "shell-local" {
      name             = "verify-boot"
      inline_shebang   = "/bin/bash"
      environment_vars = ["MACHINE=${local.variant_config[source.name].machine}"]
      inline = [
        "set -euxo pipefail",
        "log=\"test/out/$$MACHINE.${var.ubuntu_name}._launch.output.ansi\"",
        "if ! test/launch.py --machine \"$$MACHINE\" --ubuntu ${var.ubuntu_name} --timeout 300 --exit-after-ready --image-dir ${var.output_base}/${source.name}; then",
        "  echo \"--- verify-boot failed; dumping $$log ---\" >&2",
        "  [ -f \"$$log\" ] && tail -200 \"$$log\" >&2",
        "  exit 1",
        "fi",
      ]
    }

    # Compress shipped disks in-place. qemu's disk_compression flag only
    # covers the primary VMName disk (deleted above); additional disks
    # need this loop. No-op on Linux: artifacts land on a zstd-compressed
    # ZFS dataset so qcow2-level compression is pure CPU waste.
    post-processor "shell-local" {
      name           = "compress"
      inline_shebang = "/bin/bash"
      inline = [
        "set -euxo pipefail",
        "if [ \"$$(uname -s)\" = \"Linux\" ]; then exit 0; fi",
        "for disk in ${var.output_base}/${source.name}/packer-ubuntu-*; do",
        "  echo \"==> compressing $$(basename \"$$disk\")\"",
        "  qemu-img convert -W -c -O qcow2 -o compression_type=zstd \"$$disk\" \"$$disk.tmp\"",
        "  mv \"$$disk.tmp\" \"$$disk\"",
        "done",
      ]
    }

    # Install: atomic-rename the per-source output out of the staging
    # tmpdir into its final home. Drops the previous good artifact
    # right before the move so the window where neither exists is one
    # rename. mise-tasks/packer/build rmdirs the (now empty) tmpdir
    # after the build returns.
    post-processor "shell-local" {
      name           = "install"
      inline_shebang = "/bin/bash"
      inline = [
        "set -euxo pipefail",
        "rm -rf ${var.artifact_base}/${source.name}",
        "mv ${var.output_base}/${source.name} ${var.artifact_base}/${source.name}",
      ]
    }
  }

}
