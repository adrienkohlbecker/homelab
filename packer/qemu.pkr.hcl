packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

locals {
  qemu_binary      = "/usr/bin/qemu-system-aarch64"
  output_directory = "${path.root}/artifacts"
  machine_type     = "virt"
  iso_checksum     = "file:https://cdimage.ubuntu.com/releases/24.04.2/release/SHA256SUMS"
  iso_url          = "https://cdimage.ubuntu.com/releases/24.04.2/release/ubuntu-24.04.2-live-server-arm64.iso"
}

# locals {
#   qemu_binary      = "/usr/bin/qemu-system-amd64"
#   output_directory = "/mnt/scratch/qemu"
#   machine_type     = "q35"
#   iso_checksum     = "file:https://releases.ubuntu.com/24.04/SHA256SUMS"
#   iso_url          = "https://releases.ubuntu.com/24.04/ubuntu-24.04.2-live-server-amd64.iso"
# }

source "qemu" "ubuntu" {
  accelerator          = "kvm"
  boot_wait            = "10s"
  cpu_model            = "host"
  cpus                 = 2
  disk_cache           = "none"
  disk_compression     = true
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
  output_directory     = "${local.output_directory}/${source.name}"
  qemu_binary          = "${local.qemu_binary}"
  shutdown_command     = "sudo /usr/sbin/shutdown -h now"
  ssh_private_key_file = "${path.root}/vagrant.key"
  ssh_timeout          = "20m"
  ssh_username         = "vagrant"
}

build {

  source "qemu.ubuntu" {
    name         = "ubuntu-base"
    boot_command = ["c<wait>linux /casper/vmlinuz --- autoinstall ds=\"nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/\"<enter><wait>", "initrd /casper/initrd<enter><wait>", "boot<enter><wait>"]
    iso_checksum = "${local.iso_checksum}"
    iso_url      = "${local.iso_url}"

  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    inline          = ["whoami"]
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    scripts         = ["${path.root}/scripts/cleanup.sh"]
    pause_after     = "2s"
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${local.output_directory}/${source.name}/{{.ChecksumType}}sum"
  }

}

build {

  source "qemu.ubuntu" {
    name                 = "ubuntu-box"
    disk_additional_size = ["40G"]
    disk_image           = true
    iso_checksum         = "file:${local.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${local.output_directory}/ubuntu-base/packer-ubuntu"
  }
  source "qemu.ubuntu" {
    name                 = "ubuntu-lab"
    disk_image           = true
    disk_additional_size = ["40G", "40G", "40G", "1G", "1G", "1.5G", "1.5G", "1G", "1G"]
    iso_checksum         = "file:${local.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${local.output_directory}/ubuntu-base/packer-ubuntu"
  }
  source "qemu.ubuntu" {
    name                 = "ubuntu-pug"
    disk_image           = true
    disk_additional_size = ["40G", "1G", "1G"]
    iso_checksum         = "file:${local.output_directory}/ubuntu-base/sha256sum"
    iso_url              = "${local.output_directory}/ubuntu-base/packer-ubuntu"
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    inline      = ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"]
    pause_after = "30s"
    env = {
      "SOURCE_NAME" = "${source.name}"
    }
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -HE bash '{{ .Path }}'"
    scripts         = ["${path.root}/scripts/postreboot.sh", "${path.root}/scripts/packer_extras.sh", "${path.root}/scripts/cleanup.sh"]
    pause_after     = "2s"
    env = {
      "SOURCE_NAME" = "${source.name}"
    }
  }

  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "truncate -s0 ${local.output_directory}/${source.name}/packer-${source.name}"
    ]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "${local.output_directory}/${source.name}/{{.ChecksumType}}sum"
  }

}

build {

  source "qemu.ubuntu" {
    name              = "ubuntu-test"
    disk_image        = true
    efi_firmware_vars = "${local.output_directory}/ubuntu-box/efivars.fd"
    iso_checksum      = "file:${local.output_directory}/ubuntu-box/sha256sum"
    iso_url           = "${local.output_directory}/ubuntu-box/packer-ubuntu-box-1"
  }

  provisioner "breakpoint" {
    disable = false
  }

}
