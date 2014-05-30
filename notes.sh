
dmesg | grep -e DMAR -e IOMMU
# as root

apt-get install virt-manager qemu-kvm
usermod -a -G libvirtd adrien

echo "blacklist radeon" >> /etc/modprobe.d/blacklist.conf

add intel_iommu=on in /etc/default/grub
modprobe pci_stub
#echo 1 > /sys/module/kvm/parameters/allow_unsafe_assigned_interrupts
echo "options kvm allow_unsafe_assigned_interrupts=1" > /etc/modprobe.d/kvm.conf

# 07:00.0 0300: 1002:6819
# 07:00.1 0403: 1002:aab0
# 00:01.0 0604: 8086:0c01

echo "1002 6819" > /sys/bus/pci/drivers/pci-stub/new_id
echo 0000:07:00.0 > /sys/bus/pci/devices/0000:07:00.0/driver/unbind
echo 0000:07:00.0 > /sys/bus/pci/drivers/pci-stub/bind

echo "1002 aab0" > /sys/bus/pci/drivers/pci-stub/new_id
echo 0000:07:00.1 > /sys/bus/pci/devices/0000:07:00.1/driver/unbind
echo 0000:07:00.1 > /sys/bus/pci/drivers/pci-stub/bind

echo "8086 0c01" > /sys/bus/pci/drivers/pci-stub/new_id
echo 0000:01:00.0 > /sys/bus/pci/devices/0000:01:00.0/driver/unbind
echo 0000:01:00.0 > /sys/bus/pci/drivers/pci-stub/bind

wget https://www.kernel.org/pub/linux/kernel/v3.x/testing/linux-3.15-rc6.tar.gz
tar -xzf linux-3.15-rc6.tar.gz
cd linux-3.15-rc6/
sudo apt-get build-dep linux-image-$(uname -r)
cp /boot/config-$(uname -r) .config
sudo apt-get install kernel-package
export CONCURRENCY_LEVEL=9
make-kpkg --initrd --append-to-version=kvm.1 kernel_image kernel_headers



iommu=1 intel_iommu=on kvm.ignore_msrs=1 nomodeset pci-stub.ids=1002:6819,1002:aab0,1912:0015,1000:0086 vfio_iommu_type1.allow_unsafe_interrupts=1 pcie_acs_override=downstream

#LSGROUP

#!/bin/sh
BASE="/sys/kernel/iommu_groups"
for i in $(find $BASE -maxdepth 1 -mindepth 1 -type d); do
GROUP=$(basename $i)
echo "### Group $GROUP ###"
for j in $(find $i/devices -type l); do
 DEV=$(basename $j)
 echo -n "    "
 lspci -s $DEV
done
done



sudo vfio-bind 0000:07:00.0 0000:07:00.1 0000:01:00.0 0000:02:00.0
sudo qemu-system-x86_64 -enable-kvm -M q35 -m 1024 -cpu host \
-smp 6,sockets=1,cores=6,threads=1 \
-boot menu=on \
-serial null \
-parallel null \
-display none \
-monitor none \
-bios /usr/share/qemu/bios.bin -vga none \
-device ioh3420,bus=pcie.0,addr=1c.0,multifunction=on,port=1,chassis=1,id=root.1 \
-device vfio-pci,host=07:00.0,bus=root.1,addr=00.0,multifunction=on,x-vga=on \
-device vfio-pci,host=07:00.1,bus=root.1,addr=00.1 \
-device virtio-scsi-pci,id=scsi \
-drive file=/var/lib/libvirt/images/HTPC-2-clone.img,id=disk,format=raw -device scsi-hd,drive=disk \
-drive file=/home/adrien/virtio-win-0.1-74.iso,id=virtiocd -device ide-cd,bus=ide.1,drive=virtiocd \
-drive file=/home/adrien/Windows\ 7\ Professional\ SP1\ x64\ EN.iso,id=windowscd -device ide-cd,bus=ide.2,drive=windowscd \
-net nic,model=virtio -net user \
-device ahci,bus=pcie.0,id=ahci \
-device vfio-pci,host=01:00.0,bus=pcie.0 \
-usb -usbdevice host:046d:c52b






I'd guess you need to change a few things in /etc/libvirt/qemu.conf
user & group should probably be root
clear_emulator_capabilities should probably be 0
+ cgroup_acl /dev/vfio/13 /dev/vfio/1



sudo qemu-system-x86_64 -enable-kvm -M q35 -m 4096 -cpu host \
-smp 2,sockets=1,cores=6,threads=1 \
-boot menu=on \
-serial null \
-parallel null \
-bios /usr/share/qemu/bios.bin -vga none \
-device vfio-pci,host=02:00.0,bus=pcie.0
