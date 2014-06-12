sudo qemu-system-x86_64 -enable-kvm  \
    -m 2048 -cpu core2duo -machine q35 \
    -smp 2 \
    -usbdevice keyboard \
    -usbdevice mouse \
    -vga std \
    -device isa-applesmc,osk="ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc" \
    -bios /home/adrien/bios-mac.bin \
    -kernel /home/adrien/chameleon_svn2360_boot \
    -device ide-drive,bus=ide.1,drive=MacCD \
    -drive id=MacCD,if=none,cache=none,file=/home/adrien/Mavericks.iso \
    -device ide-drive,bus=ide.2,drive=MacHDD \
    -drive id=MacHDD,if=none,cache=none,file=/var/lib/libvirt/images/OSX-1.img \
    -k fr \
    -net nic,model=virtio,netdev=net0,macaddr=DE:AD:BE:EF:CA:FE \
    -netdev tap,id=net0

# http://blog.definedcode.com/osx-qemu-kvm
# http://blog.ostanin.org/2014/02/11/playing-with-mac-os-x-on-kvm/
