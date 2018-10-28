ansible-apply.%:
	(cd ansible; aws-vault exec home -- env OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ansible-playbook --inventory hosts.ini --limit $* --diff ${ANSIBLE_OPTS} playbook.yml)

ansible-check.%:
	(cd ansible; aws-vault exec home -- env OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ansible-playbook --inventory hosts.ini --limit $* --diff --check ${ANSIBLE_OPTS} playbook.yml)

packer-base:
	rm -rf output-hypervisor-base
	packer build -only hypervisor-base packer/box.json

packer-base-with-disks:
	rm -rf output-hypervisor-base-with-disks
	cp -R output-hypervisor-base output-hypervisor-base-with-disks
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-base-with-disks/mirror-1.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-base-with-disks/mirror-2.vmdk
	echo 'scsi0:1.fileName = "mirror-1.vmdk"' >> output-hypervisor-base-with-disks/packer-hypervisor-base.vmx
	echo 'scsi0:1.present = "TRUE"'           >> output-hypervisor-base-with-disks/packer-hypervisor-base.vmx
	echo 'scsi0:2.fileName = "mirror-2.vmdk"' >> output-hypervisor-base-with-disks/packer-hypervisor-base.vmx
	echo 'scsi0:2.present = "TRUE"'           >> output-hypervisor-base-with-disks/packer-hypervisor-base.vmx

packer-zfs: packer-base-with-disks
	rm -rf output-hypervisor-zfs
	packer build -only hypervisor-zfs packer/box.json

packer-zfs-without-root:
	rm -rf output-hypervisor-zfs-without-root
	cp -R output-hypervisor-zfs output-hypervisor-zfs-without-root

	# drop root volume, put mirror in first place
	rm -rf output-hypervisor-zfs-without-root/disk-cl1*
	sed -E -i 's|scsi0:0.filename = (.*)|scsi0:0.fileName = "mirror-1-cl1.vmdk"|' output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	sed -E -i 's|scsi0:1.filename = (.*)|scsi0:1.fileName = "mirror-2-cl1.vmdk"|' output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	sed -E -i 's|scsi0:2(.*)||'                                               output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx

	# add tank volumes
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-1.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-2.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-3.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-4.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-5.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/tank-6.vmdk
	echo 'scsi0:2.fileName = "tank-1.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:2.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:3.fileName = "tank-2.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:3.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:4.fileName = "tank-3.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:4.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:5.fileName = "tank-4.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:5.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:6.fileName = "tank-5.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:6.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	# scsi0:7 is reserved
	echo 'scsi0:8.fileName = "tank-6.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:8.present = "TRUE"'         >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx

	# add media volumes
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/media-1.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/media-2.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/media-3.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/media-4.vmdk
	/Applications/VMware\ Fusion.app/Contents/Library/vmware-vdiskmanager -c -s 10G -t 1 -a scsi output-hypervisor-zfs-without-root/media-5.vmdk
	echo 'scsi0:9.fileName = "media-1.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:9.present = "TRUE"'          >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:10.fileName = "media-2.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:10.present = "TRUE"'          >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:11.fileName = "media-3.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:11.present = "TRUE"'          >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:12.fileName = "media-4.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:12.present = "TRUE"'          >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:13.fileName = "media-5.vmdk"' >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx
	echo 'scsi0:13.present = "TRUE"'          >> output-hypervisor-zfs-without-root/packer-hypervisor-zfs.vmx

packer-final: packer-zfs-without-root
	rm -rf output-hypervisor-final
	packer build -only hypervisor-final packer/box.json
