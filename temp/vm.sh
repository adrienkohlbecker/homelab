#!/bin/bash

vfio-bind 0000:01:00.0 0000:01:00.1 0000:02:00.0

qemu-system-x86_64 \
-enable-kvm \
-M q35 \
-m 4G \
-cpu host \
-smp 4,sockets=1,cores=4,threads=1 \
-bios /usr/share/qemu/bios.bin \
-vga none \
-display none \
-monitor none \
-boot order=c \
-device ioh3420,bus=pcie.0,addr=1c.0,multifunction=on,port=1,chassis=1,id=root.1 \
-device vfio-pci,host=01:00.0,bus=root.1,addr=00.0,multifunction=on,x-vga=on \
-device vfio-pci,host=01:00.1,bus=root.1,addr=00.1 \
-net nic,model=virtio \
