#!/usr/bin/env python3
"""Atomic-publish a packer source artifact under an exclusive flock.

Usage: publish.py <lockfile> <src_dir> <dst_dir>

Pure-Python wrapper invoked from qemu.pkr.hcl's `install` post-processor.
Holds an exclusive fcntl.flock on <lockfile> for the duration of the
rm <dst_dir> + mv <src_dir> <dst_dir>. The test harness takes a shared
flock on the same path around _create_overlay + qemu launch (see
test/machine.py), so multiple test cells can run in parallel and only
block packer's brief publish.

Pure-Python instead of bash + flock(1) because util-linux's flock(1)
isn't on macOS by default, and `mise run packer:build` is a supported
dev-loop on Mac. fcntl.flock works identically on Linux and macOS.

The lockfile is created lazily here if missing -- packer is the first
writer of the imagedir, so the bootstrap moment is exactly this script.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <lockfile> <src_dir> <dst_dir>")
    lockfile, src, dst = sys.argv[1:4]

    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.move(src, dst)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
