packer {
  required_plugins {
    vmware = {
      version = "~> 1"
      source = "github.com/hashicorp/vmware"
    }
    vagrant = {
      source  = "github.com/hashicorp/vagrant"
      version = "~> 1"
    }
  }
}

source "vmware-iso" "ubuntu" {
  version = 21

  cpus                  = 2
  memory                = 2048

  guest_os_type         = "arm-ubuntu-64"
  iso_checksum          = "file:https://cdimage.ubuntu.com/releases/24.04.2/release/SHA256SUMS"
  iso_url               = "https://cdimage.ubuntu.com/releases/24.04.2/release/ubuntu-24.04.2-live-server-arm64.iso"

  disk_size         = 8192
  disk_adapter_type = "nvme"

  usb = "true"

  ssh_username          = "vagrant"
  ssh_private_key_file  = "${path.root}/vagrant.key"
  ssh_timeout           = "20m"

  network_adapter_type = "vmxnet3"

  output_directory = "${path.root}/output-${source.name}"
  http_directory = "${path.root}/http"

  boot_wait             = "3s"
  boot_command = [
    "e<wait>",
    "<down><down><down><end>",
    "<bs><bs><bs><bs><wait>",
    " autoinstall ds=nocloud-net\\;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ ---",
    "<wait><f10>"
  ]
  shutdown_command      = "sudo shutdown now"

  vmx_data = {
    "disk.EnableUUID" = "true"
    "usb_xhci.present" = "true"
    "keyboardAndMouseProfile" = "52b6ca63-f634-a2dc-88f6-220e6fb7dfd5" # default mac layout
  }
}

source "vmware-vmx" "ubuntu-box" {
  source_path = "${path.root}/vmx-ubuntu/packer-ubuntu.vmx"

  disk_additional_size = [ 65536 ]

  output_directory = "${path.root}/output-${source.name}"

  ssh_username          = "vagrant"
  ssh_private_key_file  = "${path.root}/vagrant.key"
  ssh_timeout           = "5m"

  shutdown_command      = "sudo shutdown now"

  boot_wait             = "30s"

  # See https://github.com/hashicorp/packer-plugin-vmware/issues/119
  vmx_data = {
    "nvme0:1.filename" = "disk-1.vmdk"
    "nvme0:1.present" = "TRUE"
  }
}

source "vmware-vmx" "ubuntu-lab" {
  source_path = "${path.root}/vmx-ubuntu/packer-ubuntu.vmx"

  disk_additional_size = [ 65536, 65536, 65536, 1024, 1024, 1536, 1536, 1024, 1024 ]

  output_directory = "${path.root}/output-${source.name}"

  ssh_username          = "vagrant"
  ssh_private_key_file  = "${path.root}/vagrant.key"
  ssh_timeout           = "5m"

  shutdown_command      = "sudo shutdown now"

  boot_wait             = "30s"

  # See https://github.com/hashicorp/packer-plugin-vmware/issues/119
  vmx_data = {
    "nvme0:1.filename" = "disk-1.vmdk"
    "nvme0:1.present" = "TRUE"
    "nvme0:2.filename" = "disk-2.vmdk"
    "nvme0:2.present" = "TRUE"
    "nvme0:3.filename" = "disk-3.vmdk"
    "nvme0:3.present" = "TRUE"
    "sata0:1.filename" = "disk-4.vmdk"
    "sata0:1.present" = "TRUE"
    "sata0:2.filename" = "disk-5.vmdk"
    "sata0:2.present" = "TRUE"
    "sata0:3.filename" = "disk-6.vmdk"
    "sata0:3.present" = "TRUE"
    "sata0:4.filename" = "disk-8.vmdk" # disk-7 is not created?
    "sata0:4.present" = "TRUE"
    "sata0:5.filename" = "disk-9.vmdk"
    "sata0:5.present" = "TRUE"
    "sata0:6.filename" = "disk-10.vmdk"
    "sata0:6.present" = "TRUE"
  }
}

source "vmware-vmx" "ubuntu-pug" {
  source_path = "${path.root}/vmx-ubuntu/packer-ubuntu.vmx"

  disk_additional_size = [ 65536, 1024, 1024 ]

  output_directory = "${path.root}/output-${source.name}"

  ssh_username          = "vagrant"
  ssh_private_key_file  = "${path.root}/vagrant.key"
  ssh_timeout           = "5m"

  shutdown_command      = "sudo shutdown now"

  boot_wait             = "30s"

  # See https://github.com/hashicorp/packer-plugin-vmware/issues/119
  vmx_data = {
    "nvme0:1.filename" = "disk-1.vmdk"
    "nvme0:1.present" = "TRUE"
    "sata0:1.filename" = "disk-2.vmdk"
    "sata0:1.present" = "TRUE"
    "sata0:2.filename" = "disk-3.vmdk"
    "sata0:2.present" = "TRUE"
  }
}

build {
  sources = ["source.vmware-iso.ubuntu"]

  provisioner "shell" {
    pause_before = "2m" # give time to processes to finish (eg snapd seems to be doing things after install)
    inline = ["sudo reboot"]
    expect_disconnect = true
    pause_after = "2m" # ensure we have time to run all the first boot things (eg cloud-init)
  }

  provisioner "shell" {
    execute_command = "echo 'vagrant' | sudo -S -H sh -c '{{ .Vars }} {{ .Path }}'"
    scripts =  ["${path.root}/scripts/cleanup.sh"]
  }

  post-processors {
    post-processor "manifest" {
      output = "${path.root}/output-${source.name}/manifest.json"
    }

    post-processor "shell-local" {
      inline_shebang = "/bin/bash"
      inline = [
          "set -euxo pipefail",
          "mkdir -p ${path.root}/vmx-${source.name}",
          "rm -rf ${path.root}/vmx-${source.name}/*",
          "jq \".builds[].files[].name\" ${path.root}/output-${source.name}/manifest.json | xargs -I {} cp -v {} ${path.root}/vmx-${source.name}"
      ]
    }

    post-processor "vagrant" {
      vagrantfile_template = "${path.root}/Vagrantfile.tmpl"
      output = "${path.root}/packer_{{.BuildName}}_{{.Provider}}_{{.Architecture}}.box"
    }
  }
}

build {
  sources = [ "source.vmware-vmx.ubuntu-lab", "source.vmware-vmx.ubuntu-box", "source.vmware-vmx.ubuntu-pug"]

  provisioner "file" {
    source = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    inline = ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision2.sh"]
  }

  provisioner "shell-local" {
    inline_shebang = "/bin/bash"
    inline = [
        "set -euxo pipefail",
        "vmrun stop ${path.root}/output-${source.name}/*.vmx soft",
        "sed -i.bak 's/nvme0:0.present = \"TRUE\"/nvme0:0.present = \"FALSE\"/' ${path.root}/output-${source.name}/*.vmx",
        "rm ${path.root}/output-${source.name}/disk-cl1*.vmdk",
        "/Applications/VMware\\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 8192M -t 1 -a nvme ${path.root}/output-${source.name}/disk-cl1.vmdk",
        "vmrun start ${path.root}/output-${source.name}/*.vmx gui"
    ]
  }

  provisioner "shell" {
    pause_before = "30s"
    execute_command = "echo 'vagrant' | sudo -S -H sh -c '{{ .Vars }} {{ .Path }}'"
    scripts =  ["${path.root}/scripts/postreboot.sh", "${path.root}/scripts/packer_extras.sh", "${path.root}/scripts/cleanup.sh"]
  }

  post-processors {
    post-processor "vagrant" {
      vagrantfile_template = "${path.root}/Vagrantfile.tmpl"
      output = "${path.root}/packer_{{.BuildName}}_{{.Provider}}_{{.Architecture}}.box"
    }
  }
}
