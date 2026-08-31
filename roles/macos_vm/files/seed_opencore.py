#!/usr/bin/env python3
"""Seed an OpenCore image without mounting it or replacing existing VM state."""

import argparse
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

APPLE_BOOT_GUID = "7C436110-AB2A-4BBB-A880-FE41995C9F82"


def run(*args: str | Path) -> str:
    """Run a bounded image operation, retaining stderr for operator diagnostics."""
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, timeout=120).stdout


def patch_config(path: Path, prev_lang_kbd: str, apple_locale: str, boot_args: str) -> None:
    """Update only Recovery defaults, preserving other plist data and its format."""
    original = path.read_bytes()
    config = plistlib.loads(original)
    config.setdefault("NVRAM", {}).setdefault("Add", {}).setdefault(APPLE_BOOT_GUID, {}).update(
        {
            "AppleLocale": apple_locale,
            "boot-args": boot_args,
            "prev-lang:kbd": prev_lang_kbd.encode("ascii"),
            "#INFO (prev-lang:kbd)": f"{prev_lang_kbd} (language:keyboard layout ID)",
        }
    )
    path.write_bytes(
        plistlib.dumps(
            config, fmt=plistlib.FMT_BINARY if original.startswith(b"bplist00") else plistlib.FMT_XML, sort_keys=False
        )
    )


def build_image(source: Path, image: Path, prev_lang_kbd: str, apple_locale: str, boot_args: str) -> None:
    """Build and validate a qcow2 in private scratch storage using the image's EFI partition."""
    raw = image.with_suffix(".raw")
    run("qemu-img", "convert", "-f", "qcow2", "-O", "raw", source, raw)
    table = json.loads(run("sfdisk", "--json", raw))["partitiontable"]
    efi = [p for p in table["partitions"] if p["type"].upper() == "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"]
    if table["label"] != "gpt" or len(efi) != 1:
        raise ValueError("OpenCore image must contain exactly one GPT EFI partition")
    drive = f"{raw}@@{efi[0]['start'] * table['sectorsize']}"
    config = image.parent / "config.plist"
    run("mcopy", "-i", drive, "::/EFI/OC/config.plist", config)
    patch_config(config, prev_lang_kbd, apple_locale, boot_args)
    run("mcopy", "-o", "-i", drive, config, "::/EFI/OC/config.plist")
    run("qemu-img", "convert", "-f", "raw", "-O", "qcow2", raw, image)
    run("qemu-img", "check", "-q", "-f", "qcow2", image)


def seed_image(source: Path, destination: Path, prev_lang_kbd: str, apple_locale: str, boot_args: str) -> bool:
    """Publish a complete image once; refuse concurrent builders and never overwrite a destination."""
    # Keep the lock inode outside disposable scratch storage and never unlink it.
    with destination.with_suffix(destination.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(destination):
            return False
        with tempfile.TemporaryDirectory(prefix=".opencore_", dir=destination.parent) as scratch:
            image = Path(scratch) / "OpenCore.qcow2"
            build_image(source, image, prev_lang_kbd, apple_locale, boot_args)
            shutil.chown(image, user="root", group="kvm")
            image.chmod(0o644)
            with image.open("rb") as stream:
                os.fsync(stream.fileno())
            # link publishes atomically and refuses even a non-cooperating writer's destination.
            os.link(image, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--prev-lang-kbd", required=True)
    parser.add_argument("--apple-locale", required=True)
    parser.add_argument("--boot-args", required=True)
    args = parser.parse_args()
    changed = seed_image(args.source, args.destination, args.prev_lang_kbd, args.apple_locale, args.boot_args)
    print("changed" if changed else "ok")


if __name__ == "__main__":
    main()
