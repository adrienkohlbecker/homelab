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
  description = "When true, pull apt packages straight from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
}

locals {
  ubuntu_versions = {
    jammy = { release = "22.04", patch = "22.04.5" }
    noble = { release = "24.04", patch = "24.04.4" }
  }
  ubuntu_release = local.ubuntu_versions[var.ubuntu_name].release
  ubuntu_patch   = local.ubuntu_versions[var.ubuntu_name].patch

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
  iso_arch          = var.arch == "x86_64" ? "amd64" : "arm64"
  # qemu's `virt` machine has no default graphics or input devices, so
  # without these the VNC display falls back to the HMP monitor and packer's
  # boot_command keystrokes get parsed as monitor commands. q35 already
  # ships with a std VGA and PS/2 keyboard, so this is aarch64-only.
  arch_qemuargs = var.arch == "aarch64" ? [
    ["-device", "virtio-gpu-pci"],
    ["-device", "qemu-xhci"],
    ["-device", "usb-kbd"],
    ["-device", "usb-tablet"],
  ] : []
  # x86_64 BIOS-style: drop into the GRUB shell with `c` and retype the
  # linux/initrd/boot lines, with autoinstall args inline.
  # arm64 EFI: GRUB shows a graphical menu instead. Press `e` to edit the
  # highlighted "Try or Install Ubuntu Server" entry; cursor lands on the
  # `setparams` line. The first <down> after entering edit mode is
  # consistently swallowed (extra <wait>s don't help, so it's a state
  # transition, not a timing issue), so we send three <down>s to land on
  # the `linux` line (rows: setparams, set gfxpayload=keep, linux). <end>
  # jumps past the existing `---` separator, append the autoinstall args,
  # then Ctrl-X to boot.
  boot_command = var.arch == "x86_64" ? [
    "c<wait>linux /casper/vmlinuz --- autoinstall ds=\"nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/\"<enter><wait>",
    "initrd /casper/initrd<enter><wait>",
    "boot<enter><wait>"
    ] : [
    "<wait>e<wait2>",
    "<down><wait><down><wait><down><wait><end><wait>",
    " autoinstall ds=\"nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/\"<wait>",
    "<leftCtrlOn>x<leftCtrlOff>"
  ]
  # x86_64 server ISOs live on releases.ubuntu.com keyed by codename;
  # arm64 server ISOs live on cdimage.ubuntu.com keyed by patch version.
  # Both upstreams are proxied through Nexus raw repos (see
  # terraform/nexus.tf raw_proxies); routes ISO + SHA256SUMS through the
  # local cache. `-var upstream_mirrors=true` bypasses Nexus same as for apt.
  upstream_iso_base = var.arch == "x86_64" ? "https://releases.ubuntu.com/${local.ubuntu_release}" : "https://cdimage.ubuntu.com/releases/${local.ubuntu_patch}/release"
  nexus_iso_base    = var.arch == "x86_64" ? "https://nexus.lab.fahm.fr/repository/ubuntu-releases/${local.ubuntu_release}" : "https://nexus.lab.fahm.fr/repository/ubuntu-cdimage/releases/${local.ubuntu_patch}/release"
  iso_base_url      = var.upstream_mirrors ? local.upstream_iso_base : local.nexus_iso_base
  iso_checksum      = "file:${local.iso_base_url}/SHA256SUMS"
  iso_url           = "${local.iso_base_url}/ubuntu-${local.ubuntu_patch}-live-server-${local.iso_arch}.iso"

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
  accelerator          = "${local.accelerator}"
  boot_wait            = "10s"
  cpu_model            = "host"
  sockets              = 8
  disk_cache           = "unsafe"
  disk_compression     = false
  disk_detect_zeroes   = "unmap"
  disk_discard         = "unmap"
  disk_interface       = "virtio"
  disk_size            = "40G"
  efi_boot             = true
  efi_firmware_code    = "${local.efi_firmware_code}"
  efi_firmware_vars    = "${local.efi_firmware_vars}"
  format               = "qcow2"
  headless             = true
  # http_content (vs http_directory) lets us render user-data through
  # templatefile() so the autoinstall picks up the build-time mirror
  # URLs resolved in locals above. meta-data is empty but cloud-init's
  # NoCloud datasource still requires the path to exist.
  http_content = {
    "/user-data" = templatefile("http/user-data.pkrtpl", {
      archive_url  = local.build_archive
      security_url = local.build_security
    })
    "/meta-data" = ""
  }
  machine_type         = "${local.machine_type}"
  memory               = 4096
  net_device           = "virtio-net"
  output_directory     = "${var.output_directory}/${source.name}.new"
  qemu_binary          = "${local.qemu_binary}"
  shutdown_command     = "sudo /usr/sbin/shutdown -h now"
  skip_compaction      = true
  ssh_private_key_file = "${path.root}/vagrant.key"
  ssh_timeout          = "20m"
  ssh_username         = "vagrant"
  vnc_bind_address     = "0.0.0.0"
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
  qmp_enable           = true
}

build {

  source "qemu.ubuntu" {
    name          = "ubuntu-base"
    boot_command  = local.boot_command
    iso_checksum  = "${local.iso_checksum}"
    iso_url       = "${local.iso_url}"
    host_port_max = 2231
    host_port_min = 2222
    vnc_port_max  = 5909
    vnc_port_min  = 5900
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    inline          = ["whoami"]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${var.output_directory}/${source.name}.new/{{.ChecksumType}}sum"
  }

  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "rm -rf ${var.output_directory}/${source.name}",
      "mv ${var.output_directory}/${source.name}.new ${var.output_directory}/${source.name}",
    ]
  }

}

build {

  # ubuntu-zfs: single-disk rpool. Consumed by the box and pug test
  # variants. Per-variant differences (extra pools like apoc) are created
  # at test boot via test/disks/<variant>.sh, not baked here.
  source "qemu.ubuntu" {
    name                 = "ubuntu-zfs"
    disk_additional_size = ["40G"]
    disk_image           = true
    iso_checksum         = "file:${var.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${var.output_directory}/ubuntu-base/packer-ubuntu"
    host_port_max        = 2241
    host_port_min        = 2232
    vnc_port_max         = 5919
    vnc_port_min         = 5910
  }

  # ubuntu-zfs-lab: mdadm-EFI + mdadm-swap + 3-disk mirror rpool. Consumed
  # by the lab test variant; matches the lab-class prod host shape. Also
  # serves as the multi-disk regression for provision.sh / chroot.sh and
  # as a copy-paste reference for provisioning new lab-class prod hosts.
  # See AGENTS.md "Test Environment Design".
  source "qemu.ubuntu" {
    name                 = "ubuntu-zfs-lab"
    disk_additional_size = ["40G", "40G", "40G"]
    disk_image           = true
    iso_checksum         = "file:${var.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${var.output_directory}/ubuntu-base/packer-ubuntu"
    host_port_max        = 2251
    host_port_min        = 2242
    vnc_port_max         = 5929
    vnc_port_min         = 5920
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    inline            = ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"]
    expect_disconnect = true
    pause_after       = "30s"
    # Mirror URLs are resolved here (HCL) and passed as env. provision.sh
    # uses UBUNTU_MIRROR* during the build; chroot.sh swaps in the
    # UBUNTU_MIRROR_*_UPSTREAM pair at the end so the shipped image
    # never points at Nexus.
    env = {
      "SOURCE_NAME"                    = "${source.name}"
      "UBUNTU_NAME"                    = "${var.ubuntu_name}"
      "UBUNTU_MIRROR"                  = local.build_archive
      "UBUNTU_MIRROR_SECURITY"         = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"         = local.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.upstream_security
    }
  }

  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "truncate -s0 ${var.output_directory}/${source.name}.new/packer-ubuntu"
    ]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${var.output_directory}/${source.name}.new/{{.ChecksumType}}sum"
  }

  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "rm -rf ${var.output_directory}/${source.name}",
      "mv ${var.output_directory}/${source.name}.new ${var.output_directory}/${source.name}",
    ]
  }

}
