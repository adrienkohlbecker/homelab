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

locals {
  ubuntu_versions = {
    jammy = { release = "22.04", patch = "22.04.5" }
    noble = { release = "24.04", patch = "24.04.4" }
  }
  ubuntu_release = local.ubuntu_versions[var.ubuntu_name].release
  ubuntu_patch   = local.ubuntu_versions[var.ubuntu_name].patch
}

# arm64 alternative:
#   qemu_binary  = "/usr/bin/qemu-system-aarch64"
#   machine_type = "virt"
#   iso_checksum = "file:https://cdimage.ubuntu.com/releases/${local.ubuntu_patch}/release/SHA256SUMS"
#   iso_url      = "https://cdimage.ubuntu.com/releases/${local.ubuntu_patch}/release/ubuntu-${local.ubuntu_patch}-live-server-arm64.iso"

locals {
  qemu_binary  = "/usr/bin/qemu-system-x86_64"
  machine_type = "q35"
  iso_checksum = "file:https://releases.ubuntu.com/${local.ubuntu_release}/SHA256SUMS"
  iso_url      = "https://releases.ubuntu.com/${local.ubuntu_release}/ubuntu-${local.ubuntu_patch}-live-server-amd64.iso"
}

source "qemu" "ubuntu" {
  accelerator          = "kvm"
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
  format               = "qcow2"
  headless             = true
  http_directory       = "${path.root}/http"
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
  qemuargs = [
    ["-object", "rng-random,id=rng0,filename=/dev/urandom"],
    ["-device", "virtio-rng-pci,rng=rng0"],
  ]
}

build {

  source "qemu.ubuntu" {
    name          = "ubuntu-base"
    boot_command  = ["c<wait>linux /casper/vmlinuz --- autoinstall ds=\"nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/\"<enter><wait>", "initrd /casper/initrd<enter><wait>", "boot<enter><wait>"]
    iso_checksum  = "${local.iso_checksum}"
    iso_url       = "${local.iso_url}"
    host_port_max = 2222
    host_port_min = 2222
    vnc_port_max  = 5900
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

  source "qemu.ubuntu" {
    name                 = "ubuntu-box"
    disk_additional_size = ["40G"]
    disk_image           = true
    iso_checksum         = "file:${var.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${var.output_directory}/ubuntu-base/packer-ubuntu"
    host_port_max        = 2223
    host_port_min        = 2223
    vnc_port_max         = 5901
    vnc_port_min         = 5901
  }
  source "qemu.ubuntu" {
    name                 = "ubuntu-lab"
    disk_image           = true
    disk_additional_size = ["40G", "40G", "40G", "1G", "1G", "1.5G", "1.5G", "1G", "1G"]
    iso_checksum         = "file:${var.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${var.output_directory}/ubuntu-base/packer-ubuntu"
    host_port_max        = 2224
    host_port_min        = 2224
    vnc_port_max         = 5902
    vnc_port_min         = 5902
  }
  source "qemu.ubuntu" {
    name                 = "ubuntu-pug"
    disk_image           = true
    disk_additional_size = ["40G", "1G", "1G"]
    iso_checksum         = "file:${var.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${var.output_directory}/ubuntu-base/packer-ubuntu"
    host_port_max        = 2225
    host_port_min        = 2225
    vnc_port_max         = 5903
    vnc_port_min         = 5903
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    inline            = ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"]
    expect_disconnect = true
    pause_after       = "30s"
    env = {
      "SOURCE_NAME" = "${source.name}"
      "UBUNTU_NAME" = "${var.ubuntu_name}"
    }
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -HE bash '{{ .Path }}'"
    scripts         = ["${path.root}/scripts/packer_extras.sh"]
    pause_after     = "2s"
    env = {
      "SOURCE_NAME" = "${source.name}"
      "UBUNTU_NAME" = "${var.ubuntu_name}"
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
