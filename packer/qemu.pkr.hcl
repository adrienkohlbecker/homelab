packer {
  required_plugins {
    qemu = {
      version = "~> 1"
      source  = "github.com/hashicorp/qemu"
    }
    external = {
      version = ">= 0.0.2"
      source  = "github.com/joomcode/external"
    }
  }
}

# Host arch detected at template-eval time. The build VM and the
# shipped image are always the same arch as documented (x86_64 =
# Linux + KVM, aarch64 = arm Mac + HVF), so the host's arch is the
# right value to feed everywhere downstream. The arch_table lookup
# at local.arch_cfg fails loudly on anything outside x86_64/aarch64.
data "external-raw" "host_arch" {
  program = ["uname", "-m"]
  query   = ""
}

variable "ubuntu_name" {
  type    = string
  default = "jammy"
}

variable "build_directory" {
  type        = string
  description = "Staging root for per-source build artifacts."
}

variable "output_directory" {
  type        = string
  description = "Parent directory for published per-source artifact dirs."
}

variable "publish" {
  type        = bool
  default     = true
  description = "When false, build and verify without publishing artifacts."
}

variable "upstream_mirrors" {
  type        = bool
  default     = false
  description = "When true, build from upstream Ubuntu mirrors instead of Nexus."
}

locals {
  # Normalize Mac's "arm64" to "aarch64" (qemu / refind / ZBM use the
  # latter; uname -m reports the former). Pass-through for x86_64.
  arch_raw = trimspace(data.external-raw.host_arch.result)
  arch     = local.arch_raw == "arm64" ? "aarch64" : local.arch_raw

  # Cloud image pins (snapshot date + sha256) live in ubuntu_images.json.
  # Bump when refreshing; older snapshots eventually fall out of the
  # upstream listing (and out of the Nexus proxy cache).
  ubuntu_images   = jsondecode(file("ubuntu_images.json"))
  ubuntu_snapshot = local.ubuntu_images[var.ubuntu_name].snapshot

  # Arch-keyed configuration table. Centralizes everything that varies
  # between the supported builds. In this stack arch is a 1:1 proxy for
  # OS (x86_64 = Linux + KVM, aarch64 = arm Mac + HVF).
  #
  # Field notes:
  # - accelerator: kvm on Linux, hvf on Mac.
  # - efi_firmware_*: ovmf on Linux, Homebrew qemu (aarch64 code + arm
  #   vars) on Mac.
  # - image_format: raw on Linux (zfs already does CoW + zstd
  #   so qcow2 stacks redundant work); qcow2 on Mac (APFS has no
  #   fs-level compression).
  # - qemuargs: aarch64's `virt` machine ships no default graphics or
  #   input devices so VNC would be blank without these. q35 already
  #   has std VGA + PS/2 keyboard, so the x86_64 list is empty.
  # - cloud_image_suffix: filename token in the upstream cloud-image
  #   tarball naming.
  # - upstream/nexus_archive/security: APT mirror URLs
  nexus_base = "http://nexus.lab.fahm.fr/repository"
  arch_table = {
    x86_64 = {
      qemu_binary  = "qemu-system-x86_64"
      machine_type = "q35"
      accelerator  = "kvm"
      # 4M variants because Ubuntu 24.04 dropped the legacy non-4M
      # OVMF_{CODE,VARS}.fd from the `ovmf` package; jammy ships both,
      # noble only the 4M ones. test/arch.py:uefi_code_candidates carries
      # the same fallback list.
      efi_firmware_code  = "/usr/share/OVMF/OVMF_CODE_4M.fd"
      efi_firmware_vars  = "/usr/share/OVMF/OVMF_VARS_4M.fd"
      image_format       = "raw"
      qemuargs           = []
      cloud_image_suffix = "amd64"
      upstream_archive   = "http://archive.ubuntu.com/ubuntu"
      upstream_security  = "http://security.ubuntu.com/ubuntu"
      nexus_archive      = "${local.nexus_base}/ubuntu-archive"
      nexus_security     = "${local.nexus_base}/ubuntu-security"
    }
    aarch64 = {
      qemu_binary       = "qemu-system-aarch64"
      machine_type      = "virt"
      accelerator       = "hvf"
      efi_firmware_code = "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
      efi_firmware_vars = "/opt/homebrew/share/qemu/edk2-arm-vars.fd"
      image_format      = "qcow2"
      qemuargs = [
        ["-device", "virtio-gpu-pci"],
        ["-device", "qemu-xhci"],
        ["-device", "usb-kbd"],
        ["-device", "usb-tablet"],
      ]
      cloud_image_suffix = "arm64"
      upstream_archive   = "http://ports.ubuntu.com/ubuntu-ports"
      upstream_security  = "http://ports.ubuntu.com/ubuntu-ports"
      nexus_archive      = "${local.nexus_base}/ubuntu-ports"
      nexus_security     = "${local.nexus_base}/ubuntu-ports"
    }
  }
  arch_cfg = local.arch_table[local.arch]

  # Per-source config consumed downstream:
  # - disks: space-delimited list of whole-disk paths the build VM
  #   exposes to provision.sh as $DISKS. These become the rpool
  #   partitioned disks. The qemu source declares disk_additional_size
  #   covering rpool + extras; this string is just the rpool slice.
  # - extra_disks: space-delimited list of the remaining attached
  #   disks (not in $DISKS). provision.sh consumes these in order for
  #   the non-rpool pools named in extra_pools.
  # - disk_sizes: array of all attached disk sizes, rpool first.
  #   disk_additional_size cardinality must equal len(disks) +
  #   len(extra_disks).
  # - layout: rpool zpool create layout token. "" for single-disk,
  #   "mirror" for an rpool mirror. Consumed by provision.sh and
  #   chroot.sh.
  # - swap_size: per-disk size of the swap partition (p3, 8200), baked on
  #   every host. Single-disk mkswaps it directly; mirror mdadm's the per-disk
  #   p3s into a raid1 (/dev/md/swap). The swap role runs it as the cold
  #   overflow behind zram (notes/swap_strategy.md). Consumed by provision.sh
  #   as $SWAP_SIZE.
  # - podman_size: per-disk size of the dedicated podman-store partition
  #   (p4). "" => no podman partition (host keeps the rpool/podman zvol -- pug
  #   until its rebuild). Single-disk bakes one plain ext4 partition; mirror
  #   bakes one per rpool disk and chroot.sh mdadm's them into a raid5
  #   (/dev/md/podman). The podman role formats + mounts it. Fixture sizes only
  #   prove the mechanism; prod sizes itself at rebuild (notes/runbooks/
  #   podman_partition_rebuild.md). Consumed by provision.sh as $PODMAN_SIZE.
  # - meta_size: per-disk size of tank's special-vdev partition (p6, mirror
  #   only). "" => no meta partition (tank has no special vdev). provision.sh
  #   ZFS-mirrors the per-disk p6s into tank's special vdev; the fixture size
  #   only proves the wiring (prod is 128G, notes/unified_disk_layout.md).
  #   Consumed by provision.sh as $META_SIZE.
  # - extra_pools: space-delimited list of non-rpool pool layouts
  #   provision.sh creates after the rpool arch-chroot completes.
  #   Layouts: apoc (mirror, 2 disks), dozer (mirror, 2 disks),
  #   tank_mouse (4 disks; tank raidz2 + mouse mirror over shared
  #   partitions, matches lab prod). Empty => rpool-only image.
  # - image_target: qemu images are harness-verified; hetzner images grow on
  #   first cloud boot and are verified by mise-tasks/packer/hetzner.sh.
  # - qemu_test_image: true only for qemu test fixtures; gates test-only
  #   kernel tuning and ambient-unit masking in chroot.sh.
  # - zfs_arc_max: optional ARC cap for small-RAM cloud images; 0 disables it.
  # Add an entry whenever a new source "qemu.ubuntu" block joins the
  # build.
  variant_config = {
    # pug: single-disk rpool + apoc mirror. Matches the pug prod host.
    pug = {
      disks           = "/dev/vdb"
      extra_disks     = "/dev/vdc /dev/vdd"
      disk_sizes      = ["40G", "1G", "1G"]
      layout          = ""
      swap_size       = "8G"
      podman_size     = ""
      meta_size       = ""
      extra_pools     = "apoc"
      image_target    = "qemu"
      qemu_test_image = true
      zfs_arc_max     = 0
    }
    # lab: mdadm-EFI + 3-disk mirror rpool + dozer mirror + tank raidz2 +
    # mouse mirror. Matches the lab prod host. The mirror layout mdadm's the
    # per-disk p3/p4 into raid1 swap (/dev/md/swap) + raid5 podman
    # (/dev/md/podman), and ZFS-mirrors the per-disk p6 (meta_size) into tank's
    # special vdev -- the prod-faithful unified layout
    # (notes/unified_disk_layout.md). Fixture sizes only prove the mechanism;
    # zram is the primary swap device (notes/swap_strategy.md).
    lab = {
      disks           = "/dev/vdb /dev/vdc /dev/vdd"
      extra_disks     = "/dev/vde /dev/vdf /dev/vdg /dev/vdh /dev/vdi /dev/vdj"
      disk_sizes      = ["40G", "40G", "40G", "1G", "1G", "1.5G", "1.5G", "1G", "1G"]
      layout          = "mirror"
      swap_size       = "8G"
      podman_size     = "4G"
      meta_size       = "2G"
      extra_pools     = "dozer tank_mouse"
      image_target    = "qemu"
      qemu_test_image = true
      zfs_arc_max     = 0
    }
    # box: single-disk rpool + a 1G flat `zee` pool. The default push-CI
    # ZFS-on-root fixture. The second pool turns box from rpool-only into a
    # multi-pool host so the zfs role's trim-timer + mount-cache loops (and
    # consumers that gate on >1 pool) run on the default cell -- folding in
    # the coverage the lab/pug AMIs used to carry (their prod-faithful
    # mirror/raidz geometry, never asserted by any role, stays on qemu only).
    # Prod producer datasets (data/media/scratch/minio/services) still land
    # flat on rpool here -- zfs_{dozer,tank}_filesystem keep their rpool
    # default, so zee activates no named consumers.
    # See notes/ci_box_multidisk_drop_lab_pug_amis.md.
    # box_deps is derived from box by `mise run packer:seed-deps`, which boots
    # box with launch.py --commit, applies packer/seed_deps.yml, and publishes
    # the result. It is not a packer source.
    # box uses the partition podman backend (podman_storage_backend=partition in
    # host_vars/box.yml): the container store is a dedicated 50G ext4 partition
    # (p4), not an rpool zvol. box is the only fixture the _site_test cell
    # converges the *whole* fleet onto, and that store must hold every service
    # image plus a storage-chown-by-maps duplicate for each fake-root service
    # (homeassistant, jellyfin, authelia, ...), hence 50G. With podman out of the
    # pool, the rpool only carries the OS + the prod producer datasets
    # (data/media/scratch/minio/services land flat on it here), so it no longer
    # needs the headroom the old 50G-zvol-inside-rpool layout demanded. vdb is
    # sized for swap(4G) + podman(50G) + a ~40G rpool; the disk_sizes total stays
    # 96G. (The earlier 96G existed to host an in-pool 50G zvol with slack; the
    # partition guarantees the 50G directly now.)
    box = {
      disks           = "/dev/vdb"
      extra_disks     = "/dev/vdc"
      disk_sizes      = ["96G", "1G"]
      layout          = ""
      swap_size       = "4G"
      podman_size     = "50G"
      meta_size       = ""
      extra_pools     = "zee"
      image_target    = "qemu"
      qemu_test_image = true
      zfs_arc_max     = 0
    }
    # hetzner: ZFS-root image for Hetzner Cloud. Small rpool disk — state is MB;
    # chroot.sh's hetzner_growpart.service grows it into cpx22's ~76G on first
    # boot.
    hetzner = {
      disks           = "/dev/vdb"
      extra_disks     = ""
      disk_sizes      = ["20G"]
      layout          = ""
      swap_size       = "4G"
      podman_size     = ""
      meta_size       = ""
      extra_pools     = ""
      image_target    = "hetzner"
      qemu_test_image = false
      zfs_arc_max     = 536870912
    }
  }

  # Single source of truth for the vagrant authorized keys. Rendered
  # into both the cloud-init seed (for the build VM) and chroot.sh's
  # vagrant authorized_keys (for the shipped install).
  vagrant_ssh_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y vagrant insecure public key",
  ]

  # Cloud image base URLs. Defaults to the Nexus proxy
  # (`terraform/nexus.tf` raw_proxies "ubuntu-cloud-images"); set
  # `-var upstream_mirrors=true` to bypass it.
  upstream_cloud_base = "https://cloud-images.ubuntu.com/${var.ubuntu_name}/${local.ubuntu_snapshot}"
  nexus_cloud_base    = "https://nexus.lab.fahm.fr/repository/ubuntu-cloud-images/${var.ubuntu_name}/${local.ubuntu_snapshot}"
  cloud_base          = var.upstream_mirrors ? local.upstream_cloud_base : local.nexus_cloud_base
  cloud_checksum      = "file:${local.cloud_base}/SHA256SUMS"
  cloud_url           = "${local.cloud_base}/${var.ubuntu_name}-server-cloudimg-${local.arch_cfg.cloud_image_suffix}.img"

  # Apt mirrors. By default the build pulls through the lab Nexus proxy
  # (`group_vars/all.yml` uses the same `repository/ubuntu-*` layout); set
  # `-var upstream_mirrors=true` to bypass it. The `upstream_*` pair is
  # always the canonical Ubuntu URL — chroot.sh writes those into the
  # final `/etc/apt/sources.list` so we don't ship Nexus-internal URLs.
  build_archive  = var.upstream_mirrors ? local.arch_cfg.upstream_archive : local.arch_cfg.nexus_archive
  build_security = var.upstream_mirrors ? local.arch_cfg.upstream_security : local.arch_cfg.nexus_security
}

source "qemu" "ubuntu" {
  accelerator        = "${local.arch_cfg.accelerator}"
  boot_wait          = "2s"
  cpu_model          = "host"
  cores              = 4
  sockets            = 1
  disk_cache         = "unsafe"
  disk_compression   = false
  disk_detect_zeroes = "unmap"
  disk_discard       = "unmap"
  disk_image         = true
  disk_interface     = "virtio"
  # Cloud-image disk gets resized to this during boot so cloud-init has
  # room to grow into. provision.sh installs onto packer-ubuntu-1..N
  # and the drop-cloudimg-disk post-processor deletes this one before
  # ship — the size only matters for the build-time pivot.
  disk_size         = "10G"
  efi_boot          = true
  efi_firmware_code = "${local.arch_cfg.efi_firmware_code}"
  efi_firmware_vars = "${local.arch_cfg.efi_firmware_vars}"
  format            = "${local.arch_cfg.image_format}"
  headless          = true
  iso_checksum      = "${local.cloud_checksum}"
  iso_url           = "${local.cloud_url}"
  # NoCloud datasource: cloud-init auto-detects an attached CD/ISO
  # labelled `cidata` containing user-data + meta-data. cd_content
  # renders these inline via templatefile() so the vagrant pubkey and
  # build-time apt mirror URLs land in the seed without an on-disk
  # template file. meta-data is empty but the file must exist.
  cd_label = "cidata"
  cd_content = {
    "user-data" = templatefile("http/user-data.pkrtpl", {
      archive_url  = local.build_archive
      security_url = local.build_security
      ssh_keys     = local.vagrant_ssh_keys
    })
    "meta-data" = ""
  }
  machine_type = "${local.arch_cfg.machine_type}"
  memory       = 4096
  net_device   = "virtio-net"
  # Shim over the arch's real emulator (which it resolves from PATH): on a
  # host with passt + qemu's `-netdev stream` (the noble ci-image) it backs
  # the build-VM NIC with passt instead of libslirp, whose UDP drops under
  # parallel-build contention flake the VM's DNS. Falls back to running qemu
  # untouched (slirp) on a dev Mac or older qemu. See the file header and
  # test/machine.py for the matching harness-side change.
  qemu_binary          = "${path.root}/qemu_net_wrapper.py"
  shutdown_command     = "sudo /usr/sbin/shutdown -h now"
  skip_compaction      = true
  ssh_private_key_file = "${path.root}/vagrant.key"
  ssh_timeout          = "20m"
  ssh_username         = "vagrant"
  # Local-only VNC. To watch a build from another host, tunnel:
  #   ssh -L 5900:127.0.0.1:5900 <build-host>
  # then connect a VNC client to localhost:5900.
  vnc_bind_address = "127.0.0.1"
  qemuargs = concat([
    ["-qemu-net-wrapper-binary", local.arch_cfg.qemu_binary],
    ["-object", "rng-random,id=rng0,filename=/dev/urandom"],
    ["-device", "virtio-rng-pci,rng=rng0"],
  ], local.arch_cfg.qemuargs)

  # QMP socket lands at <output_dir>/qmp.sock and lets the build be poked
  # out-of-band: `echo '{"execute":"qmp_capabilities"}{"execute":"system_reset"}' \
  #   | socat - UNIX-CONNECT:<output_dir>/qmp.sock` resets the guest without
  # killing qemu, which is much faster than re-running packer when iterating.
  qmp_enable = true

  # Provide port ranges so we avoid any conflict between parallel builds.
  host_port_max = 2241
  host_port_min = 2222
  vnc_port_max  = 5919
  vnc_port_min  = 5900
}

build {

  source "qemu.ubuntu" {
    name                 = "pug"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "lab"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "box"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  source "qemu.ubuntu" {
    name                 = "hetzner"
    output_directory     = "${var.build_directory}/${source.name}"
    disk_additional_size = local.variant_config[source.name].disk_sizes
  }

  provisioner "file" {
    source      = "${path.root}/scripts/"
    destination = "/home/vagrant/"
  }

  # Per-release Hetzner stock cloud.cfg files. provision.sh stages the one
  # matching $UBUNTU_NAME into the new install for the hetzner image target;
  # harmless extra ~4KB upload for the qemu fixture sources, which ignore it.
  provisioner "file" {
    source      = "${path.root}/hetzner"
    destination = "/home/vagrant/"
  }

  provisioner "shell" {
    # Resolute ships sudo-rs as the default `sudo` alternative (priority 50 vs
    # classic sudo's 40). sudo-rs silently ignores the SETENV sudoers tag, so
    # `sudo -E` strips the env block below. Switch the alternative back to
    # classic sudo (which honors SETENV + -E) on resolute only; jammy/noble
    # ship classic sudo as the default already.
    inline = concat(
      var.ubuntu_name == "resolute" ? ["sudo update-alternatives --set sudo /usr/bin/sudo.ws"] : [],
      ["chmod +x /home/vagrant/*.sh", "sudo -HE /home/vagrant/provision.sh"],
    )
    # Mirror URLs are resolved here (HCL) and passed as env. provision.sh
    # uses UBUNTU_MIRROR* during the build; chroot.sh swaps in the
    # UBUNTU_MIRROR_*_UPSTREAM pair at the end so the shipped image
    # never points at Nexus.
    env = {
      "SOURCE_NAME"                     = "${source.name}"
      "DISKS"                           = local.variant_config[source.name].disks
      "EXTRA_DISKS"                     = local.variant_config[source.name].extra_disks
      "LAYOUT"                          = local.variant_config[source.name].layout
      "SWAP_SIZE"                       = local.variant_config[source.name].swap_size
      "PODMAN_SIZE"                     = local.variant_config[source.name].podman_size
      "META_SIZE"                       = local.variant_config[source.name].meta_size
      "EXTRA_POOLS"                     = local.variant_config[source.name].extra_pools
      "UBUNTU_NAME"                     = "${var.ubuntu_name}"
      "UBUNTU_MIRROR"                   = local.build_archive
      "UBUNTU_MIRROR_SECURITY"          = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.arch_cfg.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.arch_cfg.upstream_security
      "SSH_KEY_PUB"                     = join("\n", local.vagrant_ssh_keys)
      "IMAGE_TARGET"                    = local.variant_config[source.name].image_target
      "ZFS_ARC_MAX"                     = "${local.variant_config[source.name].zfs_arc_max}"
      # true only for the qemu test fixtures (box/lab/pug); false for hetzner.
      # Gates the test-only kernel tuning + ambient-unit masking in chroot.sh.
      # A bare-metal copy-paste run of chroot.sh leaves it unset, so prod never
      # picks up either.
      "QEMU_TEST_IMAGE" = "${local.variant_config[source.name].qemu_test_image}"
    }
  }

  # Final image steps live in a script so the shell is linted and the HCL stays
  # declarative.
  post-processors {
    post-processor "shell-local" {
      name   = "finalize"
      script = "${path.root}/scripts/postprocess.sh"
      environment_vars = [
        "BUILD_DIRECTORY=${var.build_directory}",
        "SOURCE_NAME=${source.name}",
        "IMAGE_FORMAT=${local.arch_cfg.image_format}",
        "IMAGE_TARGET=${local.variant_config[source.name].image_target}",
        "UBUNTU_NAME=${var.ubuntu_name}",
        "PUBLISH=${var.publish}",
        "OUTPUT_DIRECTORY=${var.output_directory}",
      ]
    }
  }

}
