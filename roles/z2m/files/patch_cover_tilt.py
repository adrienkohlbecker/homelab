#!/usr/bin/env python3
# Inject `homeassistant.cover.tilt_{command,status}_topic: null` +
# `tilt_status_template: null` on every cover device in z2m's devices.yaml.
#
# Why: z2m's HA discovery override system (extension/homeassistant.js)
# DOES honour null-for-delete and DOES support a per-object_id `cover:`
# sub-dict in `homeassistant:`, but the merge of `device_options` into
# each device's options is SHALLOW (model/device.js: `{...device_options,
# ...deviceOptions}`). Any device that already has its own
# `homeassistant: {...}` block (e.g. a renamed cover) completely shadows
# `device_options.homeassistant`, so a global tilt-null override never
# reaches the discovery code. Patching the per-device block is the only
# place where the override actually gets applied.

import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

TILT_NULLS = {
    "tilt_status_topic": None,
    "tilt_status_template": None,
    "tilt_command_topic": None,
}


def patch(devices: dict, name_re: re.Pattern) -> bool:
    changed = False
    for dev in devices.values():
        if not isinstance(dev, dict):
            continue
        fn = dev.get("friendly_name", "")
        if not name_re.search(fn):
            continue
        ha = dev.setdefault("homeassistant", {})
        cover = ha.setdefault("cover", {})
        for k, v in TILT_NULLS.items():
            if k not in cover or cover[k] is not v:
                cover[k] = v
                changed = True
    return changed


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <friendly-name-regex> <devices.yaml>", file=sys.stderr)
        return 2
    name_re = re.compile(sys.argv[1])
    path = Path(sys.argv[2])
    raw = path.read_text()
    devices = yaml.safe_load(raw) or {}
    if not isinstance(devices, dict):
        print(f"unexpected root type {type(devices).__name__} in {path}", file=sys.stderr)
        return 1
    if not patch(devices, name_re):
        print("OK")
        return 0
    with NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        yaml.safe_dump(devices, tmp, default_flow_style=False, sort_keys=False, allow_unicode=True)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(path.stat().st_mode & 0o777)
    tmp_path.replace(path)
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
