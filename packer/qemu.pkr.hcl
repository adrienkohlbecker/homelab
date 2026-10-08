# Keep `inline` shell to one or two simple commands. Longer provisioner logic
# goes in a checked-in .sh under packer/scripts/ (with `set -euo pipefail`), so
# shellcheck and shfmt cover it and this stays declarative. packer:build
# verifies and publishes each built source after packer returns.

packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

# Native host architecture and operating system are separate dimensions: both
# Linux/KVM and macOS/HVF can build aarch64 images. The table lookups below fail
# loudly for unsupported values.
variable "host_arch" {
  type        = string
  description = "Build host `uname -m`."
}

variable "host_os" {
  type        = string
  description = "Build host `uname -s`."
}

variable "ubuntu_name" {
  type    = string
  default = null
}

variable "build_directory" {
  type        = string
  description = "Staging root for per-source build artifacts."
}

variable "image_format" {
  type        = string
  description = "Disk image format: raw or qcow2."
}

variable "upstream_mirrors" {
  type        = bool
  default     = false
  description = "When true, build from upstream Ubuntu mirrors instead of Nexus."
}

locals {
  # Normalize Mac's "arm64" to "aarch64" (qemu / refind / ZBM use the
  # latter; uname -m reports the former). Pass-through for x86_64.
  arch     = var.host_arch == "arm64" ? "aarch64" : var.host_arch
  host_os  = lower(var.host_os)
  versions = yamldecode(file("${path.cwd}/group_vars/all/versions.yml"))

  # Codename -> Ubuntu version and immutable released-image serial.
  ubuntu_catalog = yamldecode(file("${path.cwd}/data/ubuntu_releases.yml"))
  ubuntu_name    = coalesce(var.ubuntu_name, local.ubuntu_catalog.default)
  ubuntu_release = local.ubuntu_catalog.releases[local.ubuntu_name]
  ubuntu_version = local.ubuntu_release.version

  # Guest machine, NIC, and cloud-image token are shared with the qemu harness.
  architectures = yamldecode(file("${path.cwd}/data/architectures.yml"))
  guest         = local.architectures[local.arch].guest

  # aarch64's `virt` machine ships no default graphics or input devices, so VNC
  # would be blank without these. q35 already has std VGA + PS/2 keyboard.
  display_qemuargs = {
    x86_64 = []
    aarch64 = [
      ["-device", "virtio-gpu-pci"],
      ["-device", "qemu-xhci"],
      ["-device", "usb-kbd"],
      ["-device", "usb-tablet"],
    ]
  }[local.arch]

  accelerator = { linux = "kvm", darwin = "hvf" }[local.host_os]

  # UEFI firmware for this host OS; unsupported pairs (x86_64 on darwin) have
  # no entry and fail the lookup.
  firmware_cfg = local.guest.uefi_firmware[local.host_os]

  # Each qemu source below has one entry. disk_sizes covers every attached disk
  # in device order; env is the variant's part of provision.sh's environment.
  # The space-delimited DISKS prefix becomes rpool and EXTRA_DISKS supplies the
  # remaining devices to EXTRA_POOLS in order. Supported layouts are "" and
  # mirror; EXTRA_POOLS accepts apoc, dozer, and tank_mouse. Empty optional
  # fields disable their feature. The source name selects qemu versus Hetzner
  # installation. Keep lab explicit here to document the physical host in the
  # rack; hetzner is the single-disk build.
  variants = {
    # lab: mdadm EFI/swap/podman, 3-disk mirror rpool, dozer mirror, tank raidz2
    # + special mirror, and mouse mirror. The podman RAID5 needs room for the
    # full-site container fleet; the dozer mirror needs transcode scratch space.
    # Prod sizing lives in notes/unified_disk_layout.md.
    lab = {
      disk_sizes = ["60G", "60G", "60G", "4G", "4G", "1.5G", "1.5G", "1G", "1G"]
      env = {
        DISKS       = "/dev/vdb /dev/vdc /dev/vdd"
        EXTRA_DISKS = "/dev/vde /dev/vdf /dev/vdg /dev/vdh /dev/vdi /dev/vdj"
        LAYOUT      = "mirror"
        SWAP_SIZE   = "8G"
        PODMAN_SIZE = "25G"
        META_SIZE   = "2G"
        EXTRA_POOLS = "dozer tank_mouse"
      }
    }
    # hetzner: ZFS-root image for Hetzner Cloud. The 40G Podman partition must
    # be present in the image because p5 follows it and ZFS cannot be shrunk or
    # moved on first boot. cloud-init's growpart (99-hetzner.cfg) grows p5 into
    # the cpx22's remaining ~16G on first boot.
    hetzner = {
      disk_sizes = ["60G"]
      env = {
        DISKS       = "/dev/vdb"
        EXTRA_DISKS = ""
        LAYOUT      = ""
        SWAP_SIZE   = "4G"
        PODMAN_SIZE = "40G"
        META_SIZE   = ""
        EXTRA_POOLS = ""
      }
    }
  }

  # Single source of truth for the vagrant authorized keys. Rendered
  # into both the cloud-init seed (for the build VM) and chroot.sh's
  # vagrant authorized_keys (for the shipped install).
  vagrant_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y vagrant insecure public key",
  ]

  # The build pulls the cloud image and apt packages through the lab Nexus
  # proxy unless `-var upstream_mirrors=true`. Like group_vars' nexus_url, an
  # empty host means upstream; provision.sh derives the apt mirror URLs from it
  # and always ships the upstream ones.
  nexus_url = var.upstream_mirrors ? "" : "nexus.lab.fahm.fr"

  # Canonical retains dated release builds, unlike the short-lived daily image
  # stream. Pinning that immutable directory keeps the image and SHA256SUMS
  # coherent even when Nexus caches their raw paths at different times.
  cloud_release_path = "releases/${local.ubuntu_name}/release-${local.ubuntu_release.image_release}"
  cloud_base         = local.nexus_url == "" ? "https://cloud-images.ubuntu.com/${local.cloud_release_path}" : "https://${local.nexus_url}/repository/ubuntu-cloud-images/${local.cloud_release_path}"
  cloud_checksum     = "file:${local.cloud_base}/SHA256SUMS"
  cloud_url          = "${local.cloud_base}/ubuntu-${local.ubuntu_version}-server-cloudimg-${local.guest.cloud_image_suffix}.img"
}

source "qemu" "ubuntu" {
  accelerator        = local.accelerator
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
  # and packer:build deletes this one before publishing — the size only
  # matters for the build-time pivot.
  disk_size         = "10G"
  efi_boot          = true
  efi_firmware_code = local.firmware_cfg.code
  efi_firmware_vars = local.firmware_cfg.vars
  format            = var.image_format
  headless          = true
  iso_checksum      = local.cloud_checksum
  iso_url           = local.cloud_url
  # NoCloud datasource: cloud-init auto-detects an attached CD/ISO
  # labelled `cidata` containing user-data + meta-data. cd_content
  # renders the vagrant pubkey into user-data via templatefile().
  # meta-data is empty but the file must exist.
  cd_label = "cidata"
  cd_content = {
    "user-data" = templatefile("http/user-data.pkrtpl", {
      ssh_keys = local.vagrant_ssh_keys
    })
    "meta-data" = ""
  }
  machine_type         = local.guest.machine_type
  memory               = 4096
  net_device           = local.guest.net_device
  qemu_binary          = "qemu-system-${local.arch}"
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
    # The plugin's default forward binds every interface, which exposes the
    # build VM's Vagrant-key sshd to anything that reaches the build host.
    ["-netdev", "user,id=user.0,hostfwd=tcp:127.0.0.1:{{ .SSHHostPort }}-:22"],
    ["-object", "rng-random,id=rng0,filename=/dev/urandom"],
    ["-device", "virtio-rng-pci,rng=rng0"],
  ], local.display_qemuargs)

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
    name                 = "lab"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variants[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "hetzner"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variants[source.name].disk_sizes
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  # Hetzner image setup: cloud-init drop-in, datasource and growpart pin, and
  # install script. provision.sh stages the whole
  # packer/hetzner dir into the build VM; chroot.sh runs install.sh inside
  # the target root.
  provisioner "file" {
    only        = ["qemu.hetzner"]
    source      = "${path.root}/hetzner"
    destination = "/home/vagrant/"
  }

  # Bootstrap files shared with their owning Ansible roles, plus the harness
  # journal mirror the qemu fixtures bake. Upload the exact working-tree bytes
  # rather than packaging the repository around them.
  provisioner "file" {
    sources = [
      "${path.cwd}/roles/boot/files/modules_most",
      "${path.cwd}/roles/boot/files/dracut_host.conf",
      "${path.cwd}/roles/console/files/console-setup",
      "${path.cwd}/roles/console/files/keyboard",
      "${path.cwd}/test/homelab_guest_journal.service",
    ]
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    # `env` applies the block below after sudo has reset the environment, so
    # neither classic sudo nor Resolute's sudo-rs needs to preserve it.
    execute_command = "chmod +x {{ .Path }}; sudo env {{ .Vars }} {{ .Path }}"
    inline          = ["bash /home/vagrant/provision.sh"]
    env = merge(local.variants[source.name].env, {
      "UBUNTU_NAME"       = local.ubuntu_name
      "NEXUS_URL"         = local.nexus_url
      "SSH_KEY_PUB"       = join("\n", local.vagrant_ssh_keys)
      "INSTALL_TARGET"    = source.name == "hetzner" ? "hetzner" : "qemu"
      "ZBM_VERSION"       = local.versions.zfsbootmenu_release[local.arch].version
      "REFIND_DEB_URL"    = local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].url : ""
      "REFIND_DEB_SHA256" = local.ubuntu_name == "noble" ? local.versions.refind_noble_release[local.arch].sha256 : ""
    })
  }

  # Records only the sources that built successfully; packer:build finalizes
  # exactly those. The manifest's own name field is the source type (ubuntu).
  post-processor "manifest" {
    output      = "${var.build_directory}/packer-manifest.json"
    custom_data = { source = source.name }
  }

}
