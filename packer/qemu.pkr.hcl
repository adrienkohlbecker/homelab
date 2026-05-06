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

variable "output_directory" {
  type = string
}

variable "arch" {
  type        = string
  default     = "x86_64"
  description = "Host arch for the build (x86_64 or aarch64). mise.toml resolves this from `uname -m`."
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

variable "image_format" {
  type        = string
  default     = "qcow2"
  description = "Disk image format: raw on Linux (artifacts land on dozer/scratch/qemu where ZFS already does CoW + zstd, so qcow2 stacks redundant work) or qcow2 on Mac (APFS has no fs-level compression). mise-tasks/packer/build resolves this from `uname -s`."
  validation {
    condition     = contains(["raw", "qcow2"], var.image_format)
    error_message = "image_format must be raw or qcow2."
  }
}

variable "zbm_version" {
  type        = string
  description = "ZFSBootMenu version to download from Gitea. Single source of truth is mise.toml vars.zbm_version; mise tasks pass it in."
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

  # Search PATH for qemu-system-{arch}; lets the same template work on
  # Linux (`/usr/bin/qemu-system-x86_64`) and Mac (`/opt/homebrew/bin/...`)
  # without per-OS path hacks.
  qemu_binary  = "qemu-system-${var.arch}"
  machine_type = var.arch == "x86_64" ? "q35" : "virt"
  # x86_64 host is Linux + KVM; aarch64 host is arm Mac + HVF in this stack.
  # If arm64 Linux ever joins the mix, swap in an explicit OS knob here.
  accelerator = var.arch == "x86_64" ? "kvm" : "hvf"
  # EFI firmware paths follow the same Linux/Mac split as accelerator.
  # Linux: ovmf package. Mac: Homebrew qemu (aarch64 code + arm vars).
  efi_firmware_code = var.arch == "x86_64" ? "/usr/share/OVMF/OVMF_CODE.fd" : "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
  efi_firmware_vars = var.arch == "x86_64" ? "/usr/share/OVMF/OVMF_VARS.fd" : "/opt/homebrew/share/qemu/edk2-arm-vars.fd"
  # qemu's `virt` machine has no default graphics or input devices, so
  # without these the VNC display is blank. q35 already ships with a std
  # VGA and PS/2 keyboard, so this is aarch64-only.
  arch_qemuargs = var.arch == "aarch64" ? [
    ["-device", "virtio-gpu-pci"],
    ["-device", "qemu-xhci"],
    ["-device", "usb-kbd"],
    ["-device", "usb-tablet"],
  ] : []
  # rEFInd EFI binary name is per-arch. chroot.sh drops it into the
  # efibootmgr boot entry verbatim.
  refind_name = var.arch == "x86_64" ? "refind_x64.efi" : "refind_aa64.efi"
  # UEFI firmware fallback path filename per arch. chroot.sh copies
  # refind to /boot/efi/EFI/BOOT/<fallback> so a host whose NVRAM has
  # been wiped (CMOS clear, BIOS update, "Restore Defaults") still
  # boots from the ESP.
  refind_fallback_name = var.arch == "x86_64" ? "BOOTX64.EFI" : "BOOTAA64.EFI"

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
  cloud_url           = "${local.cloud_base}/${var.ubuntu_name}-server-cloudimg-${var.arch == "x86_64" ? "amd64" : "arm64"}.img"

  # Apt mirrors. By default the build pulls through the lab Nexus proxy
  # (`group_vars/all.yml` uses the same `repository/ubuntu-*` layout); set
  # `-var upstream_mirrors=true` to bypass it. The `upstream_*` pair is
  # always the canonical Ubuntu URL — chroot.sh writes those into the
  # final `/etc/apt/sources.list` so we don't ship Nexus-internal URLs.
  # x86_64 ships glibc on archive/security; aarch64 lives under ports.
  upstream_archive  = var.arch == "x86_64" ? "http://archive.ubuntu.com/ubuntu" : "http://ports.ubuntu.com/ubuntu-ports"
  upstream_security = var.arch == "x86_64" ? "http://security.ubuntu.com/ubuntu" : "http://ports.ubuntu.com/ubuntu-ports"
  nexus_archive     = var.arch == "x86_64" ? "http://nexus.lab.fahm.fr/repository/ubuntu-archive" : "http://nexus.lab.fahm.fr/repository/ubuntu-ports"
  nexus_security    = var.arch == "x86_64" ? "http://nexus.lab.fahm.fr/repository/ubuntu-security" : "http://nexus.lab.fahm.fr/repository/ubuntu-ports"
  build_archive     = var.upstream_mirrors ? local.upstream_archive : local.nexus_archive
  build_security    = var.upstream_mirrors ? local.upstream_security : local.nexus_security
}

source "qemu" "ubuntu" {
  accelerator        = "${local.accelerator}"
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
  efi_firmware_code  = "${local.efi_firmware_code}"
  efi_firmware_vars  = "${local.efi_firmware_vars}"
  format             = "${var.image_format}"
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
  machine_type         = "${local.machine_type}"
  memory               = 4096
  net_device           = "virtio-net"
  output_directory     = "${var.output_directory}"
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
  ], local.arch_qemuargs)

  # QMP socket lands at <output_dir>/qmp.sock and lets the build be poked
  # out-of-band: `echo '{"execute":"qmp_capabilities"}{"execute":"system_reset"}' \
  #   | socat - UNIX-CONNECT:<output_dir>/qmp.sock` resets the guest without
  # killing qemu, which is much faster than re-running packer when iterating
  # on bootloader/firmware bits like ZBM. Other useful commands: system_powerdown
  # (ACPI shutdown), stop / cont, screendump.
  qmp_enable = true
}

build {

  # ubuntu-zfs: single-disk rpool. Consumed by the box and pug test
  # variants. Per-variant differences (extra pools like apoc) are created
  # at test boot via test/disks/<variant>.sh, not baked here.
  source "qemu.ubuntu" {
    name                 = "ubuntu-zfs"
    disk_additional_size = ["40G"]
    host_port_max        = 2231
    host_port_min        = 2222
    vnc_port_max         = 5909
    vnc_port_min         = 5900
  }

  # ubuntu-zfs-lab: mdadm-EFI + mdadm-swap + 3-disk mirror rpool. Consumed
  # by the lab test variant; matches the lab-class prod host shape. Also
  # serves as the multi-disk regression for provision.sh / chroot.sh and
  # as a copy-paste reference for provisioning new lab-class prod hosts.
  # See AGENTS.md "Test Environment Design".
  source "qemu.ubuntu" {
    name                 = "ubuntu-zfs-lab"
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
      "UBUNTU_NAME"                     = "${var.ubuntu_name}"
      "UBUNTU_MIRROR"                   = local.build_archive
      "UBUNTU_MIRROR_SECURITY"          = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.upstream_security
      "ZBM_VERSION"                     = "${var.zbm_version}"
      "ZBM_ARCH"                        = "${var.arch}"
      "REFIND_NAME"                     = "${local.refind_name}"
      "REFIND_FALLBACK_NAME"            = "${local.refind_fallback_name}"
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
    destination = "${var.output_directory}/kernel"
  }

  provisioner "file" {
    direction   = "download"
    source      = "/home/vagrant/extracted/initrd"
    destination = "${var.output_directory}/initrd"
  }

  provisioner "file" {
    direction   = "download"
    source      = "/home/vagrant/extracted/cmdline"
    destination = "${var.output_directory}/cmdline"
  }

  # packer-ubuntu is the residual cloud-image OS disk — provision.sh
  # debootstraps onto packer-ubuntu-1..N and never writes to vda, so
  # nothing downstream consumes it. The .new -> final rename and the
  # sha256sum manifest are owned by mise-tasks/packer/build.
  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "rm -f ${var.output_directory}/packer-ubuntu",
    ]
  }

}
