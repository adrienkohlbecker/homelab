#!/usr/bin/env python3
"""Patch OpenCore config.plist NVRAM defaults."""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path

APPLE_BOOT_GUID = "7C436110-AB2A-4BBB-A880-FE41995C9F82"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_plist", type=Path)
    parser.add_argument("--prev-lang-kbd", required=True)
    parser.add_argument("--apple-locale", required=True)
    parser.add_argument("--boot-args", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    with args.config_plist.open("rb") as config_file:
        config = plistlib.load(config_file)

    nvram_add = config.setdefault("NVRAM", {}).setdefault("Add", {})
    apple_boot = nvram_add.setdefault(APPLE_BOOT_GUID, {})

    desired = {
        "AppleLocale": args.apple_locale,
        "boot-args": args.boot_args,
        "prev-lang:kbd": args.prev_lang_kbd.encode("ascii"),
    }

    changed = False
    for key, value in desired.items():
        if apple_boot.get(key) != value:
            apple_boot[key] = value
            changed = True

    info_value = f"{args.prev_lang_kbd} (language:keyboard layout ID)"
    if apple_boot.get("#INFO (prev-lang:kbd)") != info_value:
        apple_boot["#INFO (prev-lang:kbd)"] = info_value
        changed = True

    if changed:
        with args.config_plist.open("wb") as config_file:
            plistlib.dump(config, config_file, sort_keys=False)
        print("changed")
    else:
        print("ok")


if __name__ == "__main__":
    main()
