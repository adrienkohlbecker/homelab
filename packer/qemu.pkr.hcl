packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
    external = {
      version = ">= 0.0.2"
      source  = "github.com/joomcode/external"
    }
  }
}

# Native host architecture and operating system are separate dimensions: both
# Linux/KVM and macOS/HVF can build aarch64 images. The table lookups below fail
# loudly for unsupported values.
data "external-raw" "host_arch" {
  program = ["uname", "-m"]
  query   = ""
}

data "external-raw" "host_os" {
  program = ["uname", "-s"]
  query   = ""
}

variable "ubuntu_name" {
  type    = string
  default = null
}

variable "build_directory" {
  type        = string
  description = "Staging root for per-source build artifacts."
}

variable "output_directory" {
  type        = string
  description = "Parent directory for published per-source artifact dirs."
}

variable "publish" {
  type        = bool
  default     = true
  description = "When false, build and verify without publishing artifacts."
}

variable "upstream_mirrors" {
  type        = bool
  default     = false
  description = "When true, build from upstream Ubuntu mirrors instead of Nexus."
}

variable "aarch64_firmware_dir" {
  type = string
  # The verify-boot post-processor inherits the same environment variable, so
  # Packer and the harness always agree on the firmware location.
  default     = env("HOMELAB_AARCH64_FIRMWARE_DIR")
  description = "Directory containing the pinned aarch64 CODE and VARS firmware files; empty means test/firmware."
}

locals {
  # Normalize Mac's "arm64" to "aarch64" (qemu / refind / ZBM use the
  # latter; uname -m reports the former). Pass-through for x86_64.
  arch_raw = trimspace(data.external-raw.host_arch.result)
  arch     = local.arch_raw == "arm64" ? "aarch64" : local.arch_raw
  host_os  = lower(trimspace(data.external-raw.host_os.result))
  versions = yamldecode(file("${path.cwd}/group_vars/all/versions.yml"))

  # Codename -> Ubuntu version and immutable released-image serial.
  ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))
  ubuntu_name    = coalesce(var.ubuntu_name, local.ubuntu_catalog.default)
  ubuntu_release = local.ubuntu_catalog.releases[local.ubuntu_name]
  ubuntu_version = local.ubuntu_release.version

  # Guest machine, NIC, cloud-image token, and pinned firmware names are shared
  # with the qemu harness.
  architectures = yamldecode(file("${path.cwd}/data/architectures.yml"))
  guest         = local.architectures[local.arch].guest

  # Architecture also controls the build-only guest devices and mirrors. Host OS
  # independently controls the accelerator and image format.
  #
  # Field notes:
  # - qemuargs: aarch64's `virt` machine ships no default graphics or
  #   input devices so VNC would be blank without these. q35 already
  #   has std VGA + PS/2 keyboard, so the x86_64 list is empty.
  # - upstream/nexus_archive/security: APT mirror URLs
  nexus_base = "http://nexus.lab.fahm.fr/repository"
  arch_table = {
    x86_64 = {
      zbm_version       = local.versions.zfsbootmenu_release.x86_64.version
      qemuargs          = []
      upstream_archive  = "http://archive.ubuntu.com/ubuntu"
      upstream_security = "http://security.ubuntu.com/ubuntu"
      nexus_archive     = "${local.nexus_base}/ubuntu-archive"
      nexus_security    = "${local.nexus_base}/ubuntu-security"
    }
    aarch64 = {
      zbm_version = local.versions.zfsbootmenu_release.aarch64.version
      qemuargs = [
        ["-device", "virtio-gpu-pci"],
        ["-device", "qemu-xhci"],
        ["-device", "usb-kbd"],
        ["-device", "usb-tablet"],
      ]
      upstream_archive  = "http://ports.ubuntu.com/ubuntu-ports"
      upstream_security = "http://ports.ubuntu.com/ubuntu-ports"
      nexus_archive     = "${local.nexus_base}/ubuntu-ports"
      nexus_security    = "${local.nexus_base}/ubuntu-ports"
    }
  }
  arch_cfg = local.arch_table[local.arch]

  host_os_table = {
    linux = {
      accelerator = "kvm"
      # ZFS already provides CoW and zstd compression on the Linux builders.
      image_format = "raw"
    }
    darwin = {
      accelerator = "hvf"
      # APFS has no filesystem-level compression for these sparse artifacts.
      image_format = "qcow2"
    }
  }
  host_os_cfg = local.host_os_table[local.host_os]

  aarch64_firmware_dir = coalesce(var.aarch64_firmware_dir, "${path.cwd}/test/firmware")
  # The pinned ARM pair is host-independent. x86_64 keeps the packaged OVMF
  # paths used by its only supported native builder, Linux/KVM.
  firmware_table = {
    x86_64 = {
      # Ubuntu 24.04 dropped the legacy non-4M names.
      code = "/usr/share/OVMF/OVMF_CODE_4M.fd"
      vars = "/usr/share/OVMF/OVMF_VARS_4M.fd"
    }
    aarch64 = {
      code = "${local.aarch64_firmware_dir}/edk2-aarch64-code.fd"
      vars = "${local.aarch64_firmware_dir}/edk2-aarch64-vars.fd"
    }
  }
  firmware_cfg = local.firmware_table[local.arch]

  # Each qemu source below has one entry. disk_sizes covers every attached disk
  # in device order; the space-delimited disks prefix becomes rpool and
  # extra_disks supplies the remaining devices to extra_pools in order.
  # Supported layouts are "" and mirror; extra_pools accepts apoc, dozer, and
  # tank_mouse. Empty optional fields disable their feature; zfs_arc_max=0
  # disables the cap. The source name selects qemu versus Hetzner installation.
  # Keep pug and lab explicit here to document the physical hosts in the rack.
  variant_config = {
    # pug: single-disk rpool + a dedicated podman partition + apoc mirror.
    # The small fixture partition proves the prod backend without carrying the
    # full service-image footprint.
    pug = {
      disks       = "/dev/vdb"
      extra_disks = "/dev/vdc /dev/vdd"
      disk_sizes  = ["40G", "1G", "1G"]
      layout      = ""
      swap_size   = "8G"
      podman_size = "4G"
      meta_size   = ""
      extra_pools = "apoc"
      zfs_arc_max = 0
    }
    # lab: mdadm EFI/swap/podman, 3-disk mirror rpool, dozer mirror, tank raidz2
    # + special mirror, and mouse mirror. The podman RAID5 needs room for the
    # full-site container fleet; the dozer mirror needs transcode scratch space.
    # Prod sizing lives in notes/unified_disk_layout.md.
    lab = {
      disks       = "/dev/vdb /dev/vdc /dev/vdd"
      extra_disks = "/dev/vde /dev/vdf /dev/vdg /dev/vdh /dev/vdi /dev/vdj"
      disk_sizes  = ["60G", "60G", "60G", "4G", "4G", "1.5G", "1.5G", "1G", "1G"]
      layout      = "mirror"
      swap_size   = "8G"
      podman_size = "25G"
      meta_size   = "2G"
      extra_pools = "dozer tank_mouse"
      zfs_arc_max = 0
    }
    # hetzner: ZFS-root image for Hetzner Cloud. The 40G Podman partition must
    # be present in the image because p5 follows it and ZFS cannot be shrunk or
    # moved on first boot. chroot.sh's hetzner_growpart.service grows p5 into
    # the cpx22's remaining ~16G on first boot.
    hetzner = {
      disks       = "/dev/vdb"
      extra_disks = ""
      disk_sizes  = ["60G"]
      layout      = ""
      swap_size   = "4G"
      podman_size = "40G"
      meta_size   = ""
      extra_pools = ""
      zfs_arc_max = 536870912
    }
  }

  # Single source of truth for the vagrant authorized keys. Rendered
  # into both the cloud-init seed (for the build VM) and chroot.sh's
  # vagrant authorized_keys (for the shipped install).
  vagrant_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y vagrant insecure public key",
  ]

  # Canonical retains dated release builds, unlike the short-lived daily image
  # stream. Pinning that immutable directory keeps the image and SHA256SUMS
  # coherent even when Nexus caches their raw paths at different times.
  cloud_release_path  = "releases/${local.ubuntu_name}/release-${local.ubuntu_release.image_release}"
  upstream_cloud_base = "https://cloud-images.ubuntu.com/${local.cloud_release_path}"
  nexus_cloud_base    = "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images/${local.cloud_release_path}"
  cloud_base          = var.upstream_mirrors ? local.upstream_cloud_base : local.nexus_cloud_base
  cloud_checksum      = "file:${local.cloud_base}/SHA256SUMS"
  cloud_url           = "${local.cloud_base}/ubuntu-${local.ubuntu_version}-server-cloudimg-${local.guest.cloud_image_suffix}.img"

  # Apt mirrors. By default the build pulls through the lab Nexus proxy
  # (`group_vars/all/main.yml` uses the same `repository/ubuntu-*` layout); set
  # `-var upstream_mirrors=true` to bypass it. The `upstream_*` pair is
  # always the canonical Ubuntu URL — chroot.sh writes those into the
  # final `/etc/apt/sources.list` so we don't ship Nexus-internal URLs.
  build_archive  = var.upstream_mirrors ? local.arch_cfg.upstream_archive : local.arch_cfg.nexus_archive
  build_security = var.upstream_mirrors ? local.arch_cfg.upstream_security : local.arch_cfg.nexus_security
}

source "qemu" "ubuntu" {
  accelerator        = local.host_os_cfg.accelerator
  boot_wait          = "2s"
  cpu_model          = "host"
  cores              = 4
  sockets            = 1
  disk_cache         = "unsafe"
  disk_compression   = false
  disk_detect_zeroes = "unmap"
  disk_discard       = "unmap"
  disk_image         = true
  disk_interface     = "virtio"
  # Cloud-image disk gets resized to this during boot so cloud-init has
  # room to grow into. provision.sh installs onto packer-ubuntu-1..N
  # and the drop-cloudimg-disk post-processor deletes this one before
  # ship — the size only matters for the build-time pivot.
  disk_size         = "10G"
  efi_boot          = true
  efi_firmware_code = local.firmware_cfg.code
  efi_firmware_vars = local.firmware_cfg.vars
  format            = local.host_os_cfg.image_format
  headless          = true
  iso_checksum      = local.cloud_checksum
  iso_url           = local.cloud_url
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
  machine_type = local.guest.machine_type
  memory       = 4096
  net_device   = local.guest.net_device
  # Shim over the arch's real emulator (which it resolves from PATH): on a
  # host with passt + qemu's `-netdev stream` (the lab CI shell runner) it
  # backs the build-VM NIC with passt instead of libslirp, whose UDP drops
  # under parallel-build contention flake the VM's DNS. Falls back to
  # running qemu untouched (slirp) on a dev Mac or older qemu. See the file
  # header and test/machine.py for the matching harness-side change.
  qemu_binary          = "${path.root}/qemu_net_wrapper.py"
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
  # killing qemu, which is much faster than re-running packer when iterating.
  qmp_enable = true

  # Provide port ranges so we avoid any conflict between parallel builds.
  host_port_max = 2241
  host_port_min = 2222
  vnc_port_max  = 5919
  vnc_port_min  = 5900
}

build {

  source "qemu.ubuntu" {
    name                 = "pug"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "lab"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "hetzner"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  # Hetzner image setup: per-release cloud-init drop-in, datasource pin,
  # first-boot growpart unit + install script. provision.sh stages the whole
  # packer/hetzner dir into the build VM; chroot.sh runs install.sh inside
  # the target root.
  provisioner "file" {
    only        = ["qemu.hetzner"]
    source      = "${path.root}/hetzner"
    destination = "/home/vagrant/"
  }

  # Bootstrap files shared with their owning Ansible roles. Upload the exact
  # working-tree bytes rather than packaging the repository around them.
  provisioner "file" {
    sources = [
      "${path.cwd}/roles/boot/files/modules_most",
      "${path.cwd}/roles/console/files/console-setup",
      "${path.cwd}/roles/console/files/keyboard",
      "${path.cwd}/roles/refind/files/zz-stage-efi-stub",
    ]
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    # Resolute ships sudo-rs as the default `sudo` alternative (priority 50 vs
    # classic sudo's 40). sudo-rs silently ignores the SETENV sudoers tag, so
    # `sudo -E` strips the env block below. Switch the alternative back to
    # classic sudo (which honors SETENV + -E) on resolute only; noble ships
    # classic sudo as the default already.
    inline = concat(
      local.ubuntu_name == "resolute" ? ["sudo update-alternatives --set sudo /usr/bin/sudo.ws"] : [],
      ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"],
    )
    # Mirror URLs are resolved here (HCL) and passed as env. provision.sh
    # uses UBUNTU_MIRROR* during the build; chroot.sh swaps in the
    # UBUNTU_MIRROR_*_UPSTREAM pair at the end so the shipped image
    # never points at Nexus.
    env = {
      "DISKS"                           = local.variant_config[source.name].disks
      "EXTRA_DISKS"                     = local.variant_config[source.name].extra_disks
      "LAYOUT"                          = local.variant_config[source.name].layout
      "SWAP_SIZE"                       = local.variant_config[source.name].swap_size
      "PODMAN_SIZE"                     = local.variant_config[source.name].podman_size
      "META_SIZE"                       = local.variant_config[source.name].meta_size
      "EXTRA_POOLS"                     = local.variant_config[source.name].extra_pools
      "UBUNTU_NAME"                     = local.ubuntu_name
      "UBUNTU_MIRROR"                   = local.build_archive
      "UBUNTU_MIRROR_SECURITY"          = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.arch_cfg.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.arch_cfg.upstream_security
      "SSH_KEY_PUB"                     = join("\n", local.vagrant_ssh_keys)
      "INSTALL_TARGET"                  = source.name == "hetzner" ? "hetzner" : "qemu"
      "REFIND_DEB_URL"                  = local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].url : ""
      "REFIND_DEB_SHA256"               = local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].sha256 : ""
      "ZBM_VERSION"                     = local.arch_cfg.zbm_version
      "ZFS_ARC_MAX"                     = "${local.variant_config[source.name].zfs_arc_max}"
    }
  }

  # Final image steps live in a script so the shell is linted and the HCL stays
  # declarative.
  post-processors {
    post-processor "shell-local" {
      name   = "finalize"
      script = "${path.root}/scripts/postprocess.sh"
      environment_vars = [
        "BUILD_DIRECTORY=${var.build_directory}",
        "SOURCE_NAME=${source.name}",
        "IMAGE_FORMAT=${local.host_os_cfg.image_format}",
        "INSTALL_TARGET=${source.name == "hetzner" ? "hetzner" : "qemu"}",
        "UBUNTU_NAME=${local.ubuntu_name}",
        "PUBLISH=${var.publish}",
        "OUTPUT_DIRECTORY=${var.output_directory}",
      ]
    }
  }

}
