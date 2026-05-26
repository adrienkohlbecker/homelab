#!/usr/bin/env python3
"""Atomic-publish a packer source artifact under an exclusive flock.

Usage: publish.py <lockfile> <src_dir> <dst_dir>

Pure-Python wrapper invoked from qemu.pkr.hcl's `install` post-processor.
Holds an exclusive fcntl.flock on <lockfile> for the duration of a
three-step atomic rename of <src_dir> over <dst_dir>. The test harness
takes a shared flock on the same path across prepare→ensure_booted in
test/machine.py, so multiple test cells run in parallel and only block
during the brief swap.

Pure-Python instead of bash + flock(1) because util-linux's flock(1)
isn't on macOS by default, and `mise run packer:build` is a supported
dev-loop on Mac. fcntl.flock works identically on Linux and macOS.

The lockfile is created lazily here if missing -- packer is the first
writer of the imagedir, so the bootstrap moment is exactly this script.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import sys
import time

# Bounded exclusive-acquire window. A wedged shared holder (e.g. a
# harness cell whose qemu boot hung past its own --timeout) would
# otherwise stall the packer build forever; surface it as a clear
# error with a debugging hint instead.
LOCK_TIMEOUT_SEC = 300


def acquire_exclusive(fd: int, lockfile: str, deadline_sec: float) -> None:
    """Block on LOCK_EX up to deadline_sec, then exit with a diagnostic."""
    end = time.monotonic() + deadline_sec
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
            if time.monotonic() >= end:
                sys.exit(
                    f"publish-lock held >{deadline_sec:.0f}s; "
                    f"concurrent test harness wedged? check `lsof {lockfile}`"
                )
            time.sleep(0.5)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <lockfile> <src_dir> <dst_dir>")
    lockfile, src, dst = sys.argv[1:4]

    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        acquire_exclusive(fd, lockfile, LOCK_TIMEOUT_SEC)
        # Atomic 3-step publish: park the current tree under .outgoing
        # before swapping in the new one. Each rename is rename(2) on
        # the same filesystem (mise-tasks/packer/build.sh builds src under
        # ${QEMU_DIR}/.build-XXX and dst is ${QEMU_DIR}/<ubuntu>/<src>),
        # so each step is one syscall under any reader's path lookup.
        # os.replace (not shutil.move) so a future change that puts
        # staging on a different fs fails loud with EXDEV instead of
        # silently turning the publish into a multi-GB copy inside the
        # exclusive lock window.
        outgoing = f"{dst}.outgoing.{os.getpid()}"
        if os.path.exists(dst):
            os.replace(dst, outgoing)
        try:
            os.replace(src, dst)
        except Exception:
            # Restore the previous good artifact so a failed swap doesn't
            # leave dst absent -- next test boot would fail rather than
            # fall back to the old image.
            if os.path.exists(outgoing):
                os.replace(outgoing, dst)
            raise
        if os.path.exists(outgoing):
            shutil.rmtree(outgoing)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
