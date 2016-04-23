# -*- mode: ruby -*-
# vi: set ft=ruby :

[
  'vagrant-cachier',
  'vagrant-fsnotify'
].each do |plugin|
  unless Vagrant.has_plugin?(plugin)
    raise "Please install #{plugin} using 'vagrant plugin install #{plugin}'"
  end
end

AVAILABLE_MEMORY = `hostinfo`.match(/memory available: (\d+\.\d+)/)[1].to_f
AVAILABLE_CPU    = `hostinfo`.match(/(\d+) processors are logically available./)[1].to_i
VM_MEMORY        = (AVAILABLE_MEMORY * 0.2 * 1024).to_i
VM_CPU           = AVAILABLE_CPU / 2

# All Vagrant configuration is done below. The "2" in Vagrant.configure
# configures the configuration version (we support older styles for
# backwards compatibility). Please don't change it unless you know what
# you're doing.
Vagrant.configure(2) do |config|
  # The most common configuration options are documented and commented below.
  # For a complete reference, please see the online documentation at
  # https://docs.vagrantup.com.

  # Every Vagrant development environment requires a box. You can search for
  # boxes at https://atlas.hashicorp.com/search.
  config.vm.box = 'hypervisor'
  config.vm.box_url = 'file://packer/parallels.box'

  # Disable automatic box update checking. If you disable this, then
  # boxes will only be checked for updates when the user runs
  # `vagrant box outdated`. This is not recommended.
  # config.vm.box_check_update = false

  # Create a forwarded port mapping which allows access to a specific port
  # within the machine from a port on the host machine. In the example below,
  # accessing "localhost:8080" will access port 80 on the guest machine.
  # config.vm.network "forwarded_port", guest: 80, host: 8080

  # Create a private network, which allows host-only access to the machine
  # using a specific IP.
  # config.vm.network "private_network", ip: "192.168.33.10"

  # Create a public network, which generally matched to bridged network.
  # Bridged networks make the machine appear as another physical device on
  # your network.
  # config.vm.network "public_network"

  # auto_config is needed due to : https://github.com/mitchellh/vagrant/issues/7155
  config.vm.network 'private_network', type: 'dhcp', auto_config: false

  # Share an additional folder to the guest VM. The first argument is
  # the path on the host to the actual folder. The second argument is
  # the path on the guest to mount the folder. And the optional third
  # argument is a set of non-required options.
  config.vm.synced_folder '.', '/vagrant', nfs: true

  # Provider-specific configuration so you can fine-tune various
  # backing providers for Vagrant. These expose provider-specific options.
  # Example for VirtualBox:
  #
  # config.vm.provider "virtualbox" do |vb|
  #   # Display the VirtualBox GUI when booting the machine
  #   vb.gui = true
  #
  #   # Customize the amount of memory on the VM:
  #   vb.memory = "1024"
  # end
  #
  # View the documentation for the provider you are using for more
  # information on available options.
  config.vm.provider 'parallels' do |prl|
    prl.name = 'hypervisor_box'
    prl.memory = VM_MEMORY
    prl.cpus = VM_CPU
    prl.check_guest_tools = false
  end

  # Define a Vagrant Push strategy for pushing to Atlas. Other push strategies
  # such as FTP and Heroku are also available. See the documentation at
  # https://docs.vagrantup.com/v2/push/atlas.html for more information.
  # config.push.define "atlas" do |push|
  #   push.app = "YOUR_ATLAS_USERNAME/YOUR_APPLICATION_NAME"
  # end

  # Enable provisioning with a shell script. Additional provisioners such as
  # Puppet, Chef, Ansible, Salt, and Docker are also available. Please see the
  # documentation for more information about their specific syntax and use.
  # config.vm.provision "shell", inline: <<-SHELL
  #   sudo apt-get update
  #   sudo apt-get install -y apache2
  # SHELL
  config.vm.provision 'ansible' do |ansible|
    ansible.playbook = 'ansible/playbook.yml'
    ansible.extra_vars = {
      is_vagrant: true
    }
  end
  #
  # if Vagrant.has_plugin?('vagrant-cachier')
  #   config.cache.scope = :box
  #   config.cache.synced_folder_opts = {
  #     type: :nfs,
  #     # The nolock option can be useful for an NFSv3 client that wants to avoid
  #     # the NLM sideband protocol. Without this option, apt-get might hang if it
  #     # tries to lock files needed for /var/cache/* operations. All of this can
  #     # be avoided by using NFSv4 everywhere. Please note that the tcp option is
  #     # not the default.
  #     mount_options: ['rw', 'vers=3', 'tcp', 'nolock']
  #   }
  # end
end
