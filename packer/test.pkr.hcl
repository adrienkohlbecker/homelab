packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

source "qemu" "ubuntu-base" {
  accelerator          = "kvm"
  boot_command         = ["c<wait>linux /casper/vmlinuz --- autoinstall ds=\"nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/\"<enter><wait>", "initrd /casper/initrd<enter><wait>", "boot<enter><wait>"]
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
  iso_checksum         = "file:https://releases.ubuntu.com/24.04/SHA256SUMS"
  iso_url              = "https://releases.ubuntu.com/24.04/ubuntu-24.04.2-live-server-amd64.iso"
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
  sources = ["source.qemu.ubuntu-base"]

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    inline          = ["whoami"]
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    scripts =  ["${path.root}/scripts/cleanup.sh"]
    pause_after = "2s"
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "/mnt/scratch/qemu/${source.name}/{{.ChecksumType}}sum"
  }

}
