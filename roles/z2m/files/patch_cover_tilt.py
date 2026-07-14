#!/usr/bin/env python3
# Inject `homeassistant.cover.tilt_{command,status}_topic: null` +
# `tilt_status_template: null` on every shutter device in z2m's devices.yaml.
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

import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml


def patch(devices: dict) -> bool:
    changed = False
    for dev in devices.values():
        if not isinstance(dev, dict):
            continue
        # Fleet covers are Schneider S520567 roller shutters named */shutter or *_shutter; revisit for tilt-capable covers.
        if not dev.get("friendly_name", "").endswith("shutter"):
            continue
        ha = dev.setdefault("homeassistant", {})
        cover = ha.setdefault("cover", {})
        for key in ("tilt_status_topic", "tilt_status_template", "tilt_command_topic"):
            if key not in cover or cover[key] is not None:
                cover[key] = None
                changed = True
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <devices.yaml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print("OK")
        return 0
    raw = path.read_text()
    devices = yaml.safe_load(raw) or {}
    if not isinstance(devices, dict):
        print(f"unexpected root type {type(devices).__name__} in {path}", file=sys.stderr)
        return 1
    if not patch(devices):
        print("OK")
        return 0
    st = path.stat()
    with NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        yaml.safe_dump(devices, tmp, default_flow_style=False, sort_keys=False, allow_unicode=True)
        tmp.flush()
        os.fchown(tmp.fileno(), st.st_uid, st.st_gid)
        os.fchmod(tmp.fileno(), st.st_mode & 0o777)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
