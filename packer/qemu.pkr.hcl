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
  type = string
}

variable "build_directory" {
  type        = string
  description = "Staging directory packer writes into. Each source writes <build_directory>/<source-name>. mise-tasks/packer/build.sh sets this to a fresh tmpdir under HOMELAB_CI_DIR so the previous good artifacts under output_directory stay intact while the new ones build."
}

variable "output_directory" {
  type        = string
  description = "Parent directory of final per-source artifact dirs. The install post-processor renames <build_directory>/<source-name> -> <output_directory>/<source-name> after verify + compress pass."
}

variable "publish" {
  type        = bool
  default     = true
  description = "When false, skip the install post-processor (no publish to output_directory). The build, verify-boot, and compress steps still run. Used by feature-branch CI to validate packer changes without overwriting master's published artifacts."
}

variable "upstream_mirrors" {
  type        = bool
  default     = false
  description = "When true, pull apt packages and the cloud image straight from upstream Ubuntu mirrors during the build instead of via the lab Nexus proxy. The shipped image always points at upstream regardless."
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
  # - machine: the test machine spec verify-boot drives.
  # - disks: space-delimited list of whole-disk paths the build VM
  #   exposes to provision.sh as $DISKS. These become the rpool
  #   partitioned disks. The qemu source declares disk_additional_size
  #   covering rpool + extras; this string is just the rpool slice.
  # - extra_disks: space-delimited list of the remaining attached
  #   disks (not in $DISKS). pools.sh consumes these in order for
  #   the non-rpool pools named in extra_pools.
  # - disk_sizes: array of all attached disk sizes, rpool first.
  #   disk_additional_size cardinality must equal len(disks) +
  #   len(extra_disks).
  # - layout: rpool zpool create layout token. "" for single-disk,
  #   "mirror" for an rpool mirror. Consumed by provision.sh and
  #   chroot.sh.
  # - swap_size: image swap size. Single-disk bakes an 8200 partition of
  #   this size; mirror bakes an rpool zvol of it (the swap role grows it
  #   to the per-host size; see notes/swap_strategy.md). Consumed by
  #   provision.sh as $SWAP_SIZE.
  # - extra_pools: space-delimited list of non-rpool pool layouts
  #   pools.sh creates after the rpool arch-chroot completes.
  #   Layouts: apoc (mirror, 2 disks), dozer (mirror, 2 disks),
  #   tank_mouse (4 disks; tank raidz2 + mouse mirror over shared
  #   partitions, matches lab prod). Empty => rpool-only image.
  #
  # Add an entry whenever a new source "qemu.ubuntu" block joins the
  # build.
  variant_config = {
    # pug: single-disk rpool + apoc mirror. Matches the pug prod host.
    pug = {
      machine         = "pug"
      disks           = "/dev/vdb"
      extra_disks     = "/dev/vdc /dev/vdd"
      disk_sizes      = ["40G", "1G", "1G"]
      layout          = ""
      swap_size       = "8G"
      extra_pools     = "apoc"
      image_target    = "qemu"
      zfs_arc_max     = ""
      qemu_test_image = true
    }
    # lab: mdadm-EFI + 3-disk mirror rpool + dozer mirror + tank raidz2 +
    # mouse mirror. Matches the lab prod host. swap_size bakes an 8G rpool
    # zvol; the swap role grows it to 16G from host_vars (zram is the
    # primary device -- see notes/swap_strategy.md).
    lab = {
      machine         = "lab"
      disks           = "/dev/vdb /dev/vdc /dev/vdd"
      extra_disks     = "/dev/vde /dev/vdf /dev/vdg /dev/vdh /dev/vdi /dev/vdj"
      disk_sizes      = ["40G", "40G", "40G", "1G", "1G", "1.5G", "1.5G", "1G", "1G"]
      layout          = "mirror"
      swap_size       = "8G"
      extra_pools     = "dozer tank_mouse"
      image_target    = "qemu"
      zfs_arc_max     = ""
      qemu_test_image = true
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
    # Note: box_deps is derived from box via `mise run packer:seed-deps`
    # (which copies box's artifacts, boots them with launch.py --commit,
    # applies packer/seed_deps.yml, and publishes the result). It is NOT
    # a packer source — there's no point re-running provision.sh + chroot
    # for a derivation that just adds podman+nginx on top.
    box = {
      machine         = "box"
      disks           = "/dev/vdb"
      extra_disks     = "/dev/vdc"
      disk_sizes      = ["40G", "1G"]
      layout          = ""
      swap_size       = "4G"
      extra_pools     = "zee"
      image_target    = "qemu"
      zfs_arc_max     = ""
      qemu_test_image = true
    }
    # hetzner: ZFS-root image for Hetzner Cloud. Small rpool disk — state is MB;
    # chroot.sh's hetzner_growpart.service grows it into cpx22's ~76G on first
    # boot. `machine` is unused here (verify-boot is excepted below), kept only
    # for variant_config shape parity.
    hetzner = {
      machine         = "hetzner"
      disks           = "/dev/vdb"
      extra_disks     = ""
      disk_sizes      = ["20G"]
      layout          = ""
      swap_size       = "4G"
      extra_pools     = ""
      image_target    = "hetzner"
      zfs_arc_max     = "536870912"
      qemu_test_image = false
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
      "EXTRA_POOLS"                     = local.variant_config[source.name].extra_pools
      "UBUNTU_NAME"                     = "${var.ubuntu_name}"
      "UBUNTU_MIRROR"                   = local.build_archive
      "UBUNTU_MIRROR_SECURITY"          = local.build_security
      "UBUNTU_MIRROR_UPSTREAM"          = local.arch_cfg.upstream_archive
      "UBUNTU_MIRROR_SECURITY_UPSTREAM" = local.arch_cfg.upstream_security
      "SSH_KEY_PUB"                     = join("\n", local.vagrant_ssh_keys)
      "IMAGE_TARGET"                    = local.variant_config[source.name].image_target
      "ZFS_ARC_MAX"                     = local.variant_config[source.name].zfs_arc_max
      # "1" only for the qemu test fixtures (box/lab/pug); "" for hetzner. Gates
      # the test-only kernel tuning + ambient-unit masking in chroot.sh. A
      # bare-metal copy-paste run of chroot.sh leaves it unset, so prod never
      # picks up either.
      "QEMU_TEST_IMAGE" = local.variant_config[source.name].qemu_test_image ? "1" : ""
    }
  }

  # Sequential chain: drop the cloud-image disk, smoke-test the boot,
  # compress (Mac only).
  post-processors {
    # packer-ubuntu is the residual cloud-image OS disk — provision.sh
    # debootstraps onto packer-ubuntu-1..N and never writes to vda, so
    # nothing downstream consumes it.
    post-processor "shell-local" {
      name           = "drop-cloudimg-disk"
      inline_shebang = "/bin/bash"
      inline = [<<-EOT
        set -euxo pipefail
        rm -f ${var.build_directory}/${source.name}/packer-ubuntu
      EOT
      ]
    }

    # Tag each shipped disk with its on-disk format so `file`/Quick Look/
    # qemu-img-without-`-f` all identify it correctly. Runs after
    # drop-cloudimg-disk so packer-ubuntu (no number) is gone and the
    # glob only matches the OS disks. test/machine.py reads images at
    # <imagedir>/.../packer-ubuntu-N.<format>; keep this rename in sync
    # with the format suffix it appends.
    post-processor "shell-local" {
      name           = "extension"
      inline_shebang = "/bin/bash"
      inline = [<<-EOT
        set -euxo pipefail
        for disk in ${var.build_directory}/${source.name}/packer-ubuntu-*; do
          mv "$${disk}" "$${disk}.${local.arch_cfg.image_format}"
        done
      EOT
      ]
    }

    # Boot the freshly-built image and wait for systemd-fully-booted.
    # Runs before compress so a failed verify short-circuits without
    # burning CPU on a dead image. test/launch.py with --exit-after-ready
    # boots the variant, waits for SSH, runs `systemctl is-system-running
    # --wait`, and exits 0 only on state "running".
    post-processor "shell-local" {
      name = "verify-boot"
      # Skip the hetzner image: it's cloud-init-only (no vagrant user) and has no
      # entry in the harness's QEMU_MACHINE_SPECS, so launch.py can't boot+SSH-
      # verify it. It's validated by deploying the snapshot to a throwaway cpx22
      # (mise-tasks/packer/hetzner.sh).
      except         = ["qemu.hetzner"]
      inline_shebang = "/bin/bash"
      inline = [<<-EOT
        set -euxo pipefail
        test/launch.py --machine ${local.variant_config[source.name].machine} --ubuntu ${var.ubuntu_name} --timeout 300 --exit-after-ready --image-dir ${var.build_directory}/${source.name}
      EOT
      ]
    }

    # Compress shipped disks in-place. qemu's disk_compression flag only
    # covers the primary VMName disk (deleted above); additional disks
    # need this loop. No-op on Linux: artifacts land on a zstd-compressed
    # ZFS dataset so qcow2-level compression is pure CPU waste.
    post-processor "shell-local" {
      name           = "compress"
      inline_shebang = "/bin/bash"
      # OS gate via HCL-resolved image_format (raw on Linux, qcow2 on
      # Mac). HCL2's only template-string escape is for ${...} interpolation,
      # so an inline "$$(uname -s)" would reach bash as literal $$(uname -s)
      # and expand $$ to the shell PID, breaking the comparison.
      inline = [<<-EOT
        set -euxo pipefail
        if [ "${local.arch_cfg.image_format}" = "raw" ]; then exit 0; fi
        for disk in ${var.build_directory}/${source.name}/packer-ubuntu-*.qcow2; do
          echo "==> compressing $(basename "$${disk}")"
          qemu-img convert -W -c -O qcow2 -o compression_type=zstd "$${disk}" "$${disk}.tmp"
          mv "$${disk}.tmp" "$${disk}"
        done
      EOT
      ]
    }

    # Install: atomic-rename the per-source output out of the staging
    # tmpdir into its final home. publish.py wraps the rm + mv in an
    # exclusive fcntl.flock on <output_directory>/.publish-lock so a
    # test cell that's mid-launch (creating a qcow2 overlay over a
    # backing file in <output_directory>/<source>/) can't read the
    # directory while it's torn between the rm and the mv. The harness
    # takes a shared flock on the same lockfile in test/machine.py's
    # _acquire_publish_lock_shared, held across prepare→boot; shared +
    # exclusive compose so multiple test cells parallel-run and only
    # block packer's brief publish. See notes/concurrency_rework.md.
    #
    # publish.py is pure-Python (no util-linux flock(1)) so the same
    # post-processor works on Linux (lab) and macOS (dev) -- macOS
    # doesn't ship flock(1) and `mise run packer:build` is supported
    # there too.
    post-processor "shell-local" {
      name           = "install"
      inline_shebang = "/bin/bash"
      inline = [<<-EOT
        set -euxo pipefail
        if [ "${var.publish}" != "true" ]; then
          echo "==> Skipping publish (publish=false)"
          exit 0
        fi
        python3 ${path.root}/publish.py \
          "${var.output_directory}/.publish-lock" \
          "${var.build_directory}/${source.name}" \
          "${var.output_directory}/${source.name}"
      EOT
      ]
    }
  }

}
