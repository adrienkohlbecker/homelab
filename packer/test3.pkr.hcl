packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

source "qemu" "ubuntu-lab" {
  accelerator          = "kvm"
  boot_wait            = "10s"
  cpu_model            = "host"
  cpus                 = 2
  disk_additional_size = [ "40G", "40G", "40G", "1G", "1G", "1.5G", "1.5G", "1G", "1G" ]
  disk_cache           = "none"
  disk_compression     = true
  disk_detect_zeroes   = "unmap"
  disk_discard         = "unmap"
  disk_image           = true
  disk_interface       = "virtio"
  disk_size            = "40G"
  efi_boot             = true
  format               = "qcow2"
  headless             = true
  http_directory       = "${path.root}/http"
  iso_checksum         = "file:/mnt/scratch/qemu/ubuntu-base/sha256sum"
  iso_url              = "/mnt/scratch/qemu/ubuntu-base/packer-ubuntu-base"
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
  sources = ["source.qemu.ubuntu-lab"]

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    inline      = ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"]
    pause_after = "30s"
  }

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -HE bash '{{ .Path }}'"
    scripts =  ["${path.root}/scripts/postreboot.sh", "${path.root}/scripts/packer_extras.sh", "${path.root}/scripts/cleanup.sh"]
    pause_after = "2s"
  }

  post-processor "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
      "set -euxo pipefail",
      "rm /mnt/scratch/qemu/${source.name}/packer-${source.name}",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-1 /mnt/scratch/qemu/${source.name}/packer-${source.name}",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-2 /mnt/scratch/qemu/${source.name}/packer-${source.name}-1",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-3 /mnt/scratch/qemu/${source.name}/packer-${source.name}-2",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-4 /mnt/scratch/qemu/${source.name}/packer-${source.name}-3",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-5 /mnt/scratch/qemu/${source.name}/packer-${source.name}-4",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-6 /mnt/scratch/qemu/${source.name}/packer-${source.name}-5",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-7 /mnt/scratch/qemu/${source.name}/packer-${source.name}-6",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-8 /mnt/scratch/qemu/${source.name}/packer-${source.name}-7",
      "mv /mnt/scratch/qemu/${source.name}/packer-${source.name}-9 /mnt/scratch/qemu/${source.name}/packer-${source.name}-8",
      "touch /mnt/scratch/qemu/${source.name}/packer-${source.name}-9"
    ]
  }

  post-processor "checksum" {
    checksum_types = ["sha256"]
    output         = "/mnt/scratch/qemu/${source.name}/{{.ChecksumType}}sum"
  }

}
