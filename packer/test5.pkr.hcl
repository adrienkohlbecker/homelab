packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

source "qemu" "ubuntu-test" {
  accelerator          = "kvm"
  boot_wait            = "10s"
  cpu_model            = "host"
  cpus                 = 2
  disk_cache           = "none"
  disk_compression     = true
  disk_detect_zeroes   = "unmap"
  disk_discard         = "unmap"
  disk_image           = true
  disk_interface       = "virtio"
  disk_size            = "40G"
  efi_boot             = true
  efi_firmware_vars    = "/mnt/scratch/qemu/ubuntu-box/efivars.fd"
  format               = "qcow2"
  headless             = true
  http_directory       = "${path.root}/http"
  iso_checksum         = "file:/mnt/scratch/qemu/ubuntu-box/sha256sum"
  iso_url              = "/mnt/scratch/qemu/ubuntu-box/packer-ubuntu-box-1"
  machine_type         = "q35"
  memory               = 4096
  net_device           = "virtio-net"
  output_directory     = "/mnt/scratch/qemu/${source.name}"
  qemu_binary          = "/usr/bin/qemu-system-x86_64"
  shutdown_command     = "sudo /usr/sbin/shutdown -h now"
  ssh_private_key_file = "${path.root}/vagrant.key"
  ssh_timeout          = "20m"
  ssh_username         = "vagrant"
}

build {
  sources = ["source.qemu.ubuntu-test"]

  provisioner "breakpoint" {
    disable = false
  }

}
