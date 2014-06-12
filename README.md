hypervisor
==========

Encrypting the ssh key for ansible :
`openssl aes-256-cbc -a -salt -in files/ansible_rsa -out files/ansible_rsa.enc`

Decrypting the key :
`openssl aes-256-cbc -d -a -in files/ansible_rsa.enc -out files/ansible_rsa`

Provisioning a new host :

1. Add it to the `tosetup` group in `hosts.ini`. Specify the hostname to set and ansible_ssh_host for the ip
2. Run `ansible-playbook -i hosts.ini setup.yml --ask-sudo-pass`
3. Add the host to the applicable group(s), specify ansible_ssh_user & ansible_ssh_private_key_file
4. Run `ansible-playbook -i hosts.ini site.yml`

Default VM config (`ubuntu_base`)
- User deploy with authorized key
- Usual password
- Displays ip in login prompt
