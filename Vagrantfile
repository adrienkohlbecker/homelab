# vim:ft=ruby
Vagrant.configure("2") do |config|
  config.vm.define "lab" do |lab|
    lab.vm.box = 'ubuntu-lab'
    lab.vm.box_url = 'file://packer/packer_ubuntu-lab_vmware_arm64.box'
  end
  config.vm.define "pug" do |pug|
    pug.vm.box = 'ubuntu-pug'
    pug.vm.box_url = 'file://packer/packer_ubuntu-pug_vmware_arm64.box'
  end
  config.vm.define "box" do |box|
    box.vm.box = 'ubuntu-box'
    box.vm.box_url = 'file://packer/packer_ubuntu-box_vmware_arm64.box'
  end
  config.vm.define "base" do |base|
    base.vm.box = 'ubuntu-base'
    base.vm.box_url = 'file://packer/packer_ubuntu_vmware_arm64.box'
  end

  config.vm.provision "ansible" do |ansible|
    ansible.playbook = "site.yml"
  end
end
