ansible-apply.%:
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sabnzbd.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sabnzbd.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sickrage.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/headphones.ini.j2
	(cd ansible; aws-vault exec default -- env OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ansible-playbook --inventory hosts.ini --limit $* --diff ${ANSIBLE_OPTS} playbook.yml)

ansible-check.%:
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sabnzbd.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sabnzbd.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/sickrage.ini.j2
	./ansible/roles/services/files/sort_ini.py ./ansible/roles/services/templates/headphones.ini.j2
	(cd ansible; aws-vault exec default -- env OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ansible-playbook --inventory hosts.ini --limit $* --diff --check ${ANSIBLE_OPTS} playbook.yml)

packer-base:
	rm -rf output-homelab-base
	packer build -only homelab-base packer/box.json

packer-base-with-disks:
	rm -rf output-homelab-base-with-disks
	cp -R output-homelab-base output-homelab-base-with-disks
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-base-with-disks/mirror-1.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-base-with-disks/mirror-2.vmdk
	echo 'scsi0:1.fileName = "mirror-1.vmdk"' >> output-homelab-base-with-disks/packer-homelab-base.vmx
	echo 'scsi0:1.present = "TRUE"'           >> output-homelab-base-with-disks/packer-homelab-base.vmx
	echo 'scsi0:2.fileName = "mirror-2.vmdk"' >> output-homelab-base-with-disks/packer-homelab-base.vmx
	echo 'scsi0:2.present = "TRUE"'           >> output-homelab-base-with-disks/packer-homelab-base.vmx

packer-zfs: packer-base-with-disks
	rm -rf output-homelab-zfs
	packer build -only homelab-zfs packer/box.json

packer-zfs-without-root:
	rm -rf output-homelab-zfs-without-root
	cp -R output-homelab-zfs output-homelab-zfs-without-root

	# drop root volume, put mirror in first place
	rm -rf output-homelab-zfs-without-root/disk-cl1*
	sed -E -i 's|scsi0:0.filename = (.*)|scsi0:0.fileName = "mirror-1-cl1.vmdk"|' output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	sed -E -i 's|scsi0:1.filename = (.*)|scsi0:1.fileName = "mirror-2-cl1.vmdk"|' output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	sed -E -i 's|scsi0:2(.*)||'                                               output-homelab-zfs-without-root/packer-homelab-zfs.vmx

	# add data volumes
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-zfs-without-root/data-1.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-zfs-without-root/data-2.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-zfs-without-root/data-3.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-homelab-zfs-without-root/data-4.vmdk
	echo 'scsi0:2.fileName = "data-1.vmdk"' >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:2.present = "TRUE"'         >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:3.fileName = "data-2.vmdk"' >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:3.present = "TRUE"'         >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:4.fileName = "data-3.vmdk"' >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:4.present = "TRUE"'         >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:5.fileName = "data-4.vmdk"' >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx
	echo 'scsi0:5.present = "TRUE"'         >> output-homelab-zfs-without-root/packer-homelab-zfs.vmx

packer-final: packer-zfs-without-root
	rm -rf output-homelab-final
	packer build -only homelab-final packer/box.json
