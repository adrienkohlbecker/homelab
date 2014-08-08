hypervisor
==========

Encrypting the ssh key for ansible :
`openssl aes-256-cbc -a -salt -in files/ansible_rsa -out files/ansible_rsa.enc`

Decrypting the key :
`openssl aes-256-cbc -d -a -in files/ansible_rsa.enc -out files/ansible_rsa`

Provisioning a new host :

1. Add it to the `tosetup` group in `hosts.ini`. Specify the hostname to set and ansible_ssh_host for the ip
2. Run `ansible-playbook -i hosts.ini setup.yml --ask-pass --ask-sudo-pass`
3. Add the host to the applicable group(s), specify ansible_ssh_user & ansible_ssh_private_key_file
4. Run `ansible-playbook -i hosts.ini site.yml`

Default VM config (`ubuntu_base`)
- User deploy with authorized key
- Usual password
- Displays ip in login prompt

To build zfs modules manually (as root)

```
# dkms remove -m zfs -v 0.6.3 --all
# dkms remove -m spl -v 0.6.3 --all
# dkms add -m spl -v 0.6.3
# dkms add -m zfs -v 0.6.3
# dkms install -m spl -v 0.6.3
# dkms install -m zfs -v 0.6.3
```

Add to fstab for root btrfs volumes : noatime,nodiratime,compress,ssd

Check if VT-d is activated

`dmesg | grep -e DMAR -e IOMMU`

Configure zfs snapshots

```
sudo zfs set com.sun:auto-snapshot:frequent=false tank
sudo zfs set com.sun:auto-snapshot:hourly=false tank

sudo zfs set com.sun:auto-snapshot:daily=false tank/backup
```

Create datasets :
sudo zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD ...
