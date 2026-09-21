# QEMU with the HVF PSCI CPU_ON fix (packer/patches/qemu_11_0_1_hvf_cpu_on.patch, inlined
# below) so ZFSBootMenu can kexec with several vCPUs on Apple Silicon. Only the aarch64
# system emulator is built. See notes/zbm_aarch64_kexec_investigation.md, Cause 2.
class QemuHvf < Formula
  desc "QEMU aarch64 system emulator with the HVF PSCI CPU_ON fix"
  homepage "https://www.qemu.org/"
  url "https://download.qemu.org/qemu-11.0.1.tar.xz"
  sha256 "0d235f5820278d914a3155ec27af8e4258d697ea892895570807d69c0cb8cd64"
  license "GPL-2.0-only"

  keg_only "it would conflict with the stock qemu formula"

  depends_on "libtool" => :build
  depends_on "meson" => :build
  depends_on "ninja" => :build
  depends_on "pkgconf" => :build
  depends_on "python-setuptools" => :build
  depends_on "python@3.14" => :build # keep aligned with meson
  depends_on :macos

  depends_on "capstone"
  depends_on "dtc"
  depends_on "glib"
  depends_on "gnutls"
  depends_on "jpeg-turbo"
  depends_on "libpng"
  depends_on "libslirp"
  depends_on "libssh"
  depends_on "libusb"
  depends_on "lzo"
  depends_on "ncurses"
  depends_on "pixman"
  depends_on "snappy"
  depends_on "vde"
  depends_on "zstd"

  uses_from_macos "bison" => :build
  uses_from_macos "flex" => :build
  uses_from_macos "bzip2"

  patch :p1, :DATA

  def install
    ENV["LIBTOOL"] = "glibtool"

    # Same wheel handling as homebrew-core's qemu: always use brew's meson.
    rm(Dir["python/wheels/*"] - Dir["python/wheels/{pycotap,qemu_qmp}-*-none-any.whl"])

    system "./configure",
           "--prefix=#{prefix}",
           "--cc=#{ENV.cc}",
           "--host-cc=#{ENV.cc}",
           "--target-list=aarch64-softmmu",
           "--disable-bsd-user",
           "--disable-download",
           "--disable-guest-agent",
           "--disable-docs",
           "--enable-slirp",
           "--enable-capstone",
           "--enable-curses",
           "--enable-fdt=system",
           "--enable-libssh",
           "--enable-vde",
           "--enable-virtfs",
           "--enable-zstd",
           "--extra-cflags=-DNCURSES_WIDECHAR=1",
           "--disable-sdl",
           "--disable-gtk",
           "--enable-cocoa"
    system "make", "V=1", "install"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/qemu-system-aarch64 --version")
  end
end

__END__
From a4de69c1236df689de984df8d7584536d194e5ba Mon Sep 17 00:00:00 2001
From: Adrien Kohlbecker <adrien.kohlbecker@gmail.com>
Date: Mon, 21 Sep 2026 21:03:09 +0200
Subject: [PATCH] target/arm: sync vCPU state after PSCI CPU_ON reset

arm_set_cpu_on_async_work() resets the target CPU and sets its entry
point and context ID in CPUARMState, but never tells the accelerator
that the register state changed. TCG reads CPUARMState directly, and
KVM re-pushes state on reset, but HVF only writes CPUARMState back to
the hardware vCPU when cpu->vcpu_dirty is set.

A vCPU that reached PSCI_OFF is left clean: its last hv_vcpu_run() exit
was a WFI or a cancel, and flush_cpu_state() cleared the flag. A later
CPU_ON then resets and re-points only the QEMU-side copy. The vCPU
resumes in its old context, with the old MMU state and vector table, and
never runs the requested entry point.

This shows up when a guest that already onlined and offlined its
secondary CPUs boots another kernel, for example kexec on Apple Silicon:
the new kernel logs "CPUn: failed to come online" for every secondary.
Fresh vCPUs are created dirty and single-CPU guests never issue CPU_ON,
so the ordinary boot path is unaffected.

Call cpu_synchronize_post_reset() after resetting the target, in both
arm_set_cpu_on_async_work() and arm_set_cpu_on_and_reset_async_work().
It is a no-op for TCG.

Signed-off-by: Adrien Kohlbecker <adrien.kohlbecker@gmail.com>
---
 target/arm/arm-powerctl.c | 3 +++
 1 file changed, 3 insertions(+)

diff --git a/target/arm/arm-powerctl.c b/target/arm/arm-powerctl.c
index a788376..34e4c19 100644
--- a/target/arm/arm-powerctl.c
+++ b/target/arm/arm-powerctl.c
@@ -16,6 +16,7 @@
 #include "qemu/log.h"
 #include "qemu/main-loop.h"
 #include "system/tcg.h"
+#include "system/hw_accel.h"
 #include "target/arm/multiprocessing.h"
 #include "trace.h"

@@ -73,6 +74,7 @@ static void arm_set_cpu_on_async_work(CPUState *target_cpu_state,

     /* Start the new CPU at the requested address */
     cpu_set_pc(target_cpu_state, info->entry);
+    cpu_synchronize_post_reset(target_cpu_state);

     g_free(info);

@@ -182,6 +184,7 @@ static void arm_set_cpu_on_and_reset_async_work(CPUState *target_cpu_state,

     /* Initialize the cpu we are turning on */
     cpu_reset(target_cpu_state);
+    cpu_synchronize_post_reset(target_cpu_state);
     target_cpu_state->halted = 0;

     /* Finally set the power status */
--
2.51.2
