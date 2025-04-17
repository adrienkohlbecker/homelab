# vim:ft=ruby
Vagrant.configure("2") do |config|
  config.vm.define "lab" do |lab|
    lab.vm.box = 'ubuntu-lab'
    lab.vm.box_url = 'file://packer/packer_ubuntu-lab_vmware_arm64.box'
    lab.vm.network "private_network", ip: "10.234.0.2", auto_config: false
  end
  config.vm.define "pug" do |pug|
    pug.vm.box = 'ubuntu-pug'
    pug.vm.box_url = 'file://packer/packer_ubuntu-pug_vmware_arm64.box'
    pug.vm.network "private_network", ip: "10.234.0.3", auto_config: false
  end
  config.vm.define "box" do |box|
    box.vm.box = 'ubuntu-box'
    box.vm.box_url = 'file://packer/packer_ubuntu-box_vmware_arm64.box'
    box.vm.network "private_network", ip: "10.234.0.5", auto_config: false
  end
  config.vm.define "base" do |base|
    base.vm.box = 'ubuntu-base'
    base.vm.box_url = 'file://packer/packer_ubuntu_vmware_arm64.box'
  end

  config.ssh.insert_key = false

  config.vm.provider "vmware_desktop" do |vb|
    vb.gui = false
  end

  config.vm.provision "ansible" do |ansible|
    ansible.compatibility_mode = "2.0"
    ansible.playbook = "site.yml"
    ansible.groups = {
      "test" => ["lab", "pug", "box", "base"]
    }
    ansible.extra_vars = {
      vmware_test: true
    }
  end
end
