#!/usr/bin/env python3
"""Manage EFI NVRAM boot entries declaratively.

Reads desired entries from the BOOT_EFI_ENTRIES environment variable
(JSON list), compares with current NVRAM state via efibootmgr, and
converges: creates missing entries, removes stale/relabeled managed
entries, and reorders BootOrder so managed entries come first.

Entries with "multi_disk": true are expanded into per-disk variants on
hosts whose ESP is backed by an mdadm RAID1 mirror (/dev/md/efi). The
per-disk index is the mirror member's stable RaidDevice role (not its
enumeration order), so a degraded mirror keeps each survivor on its own
"(disk N)" entry instead of renumbering.

Pass --check to report what would change without modifying NVRAM.

Output: single-line JSON to stdout: {"changed": bool, "actions": [str]}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# efibootmgr/mdadm talk to firmware NVRAM via efivarfs; a flaky write can hang
# the process indefinitely, which (under become:+command:) would stall the whole
# converge. Bound every call so a stuck firmware surfaces as a caught failure.
CMD_TIMEOUT = 30


def run(cmd):
    return subprocess.run(cmd, capture_output=True, check=True, text=True, timeout=CMD_TIMEOUT).stdout.strip()


def _norm_loader(path):
    return (path or "").replace("/", "\\").lower()


def _decode_optional_data(raw):
    # efibootmgr -v renders the UTF-16LE optional data that --unicode writes byte
    # by byte, so each character's null high-byte prints as a '.' separator:
    # "root" -> "r.o.o.t.". Drop those null placeholders to recover the plain
    # cmdline. Only decode when the odd positions are all '.' (the dotted
    # signature) — other entries' optional data is left untouched.
    if len(raw) >= 2 and all(c == "." for c in raw[1::2]):
        return raw[::2]
    return raw


def _norm_options(opts):
    # Collapse whitespace so the jinja-assembled cmdline (which can emit stray or
    # doubled spaces) compares equal to efibootmgr's -v rendering. Case is kept —
    # kernel cmdline is case-sensitive.
    return re.sub(r"\s+", " ", opts or "").strip()


def _is_removable_fallback(loader):
    # \EFI\BOOT\BOOT*.EFI is the UEFI removable-media fallback the firmware boots
    # when NVRAM is empty — never prune it, even if it shares a loader with a
    # managed entry.
    return bool(re.search(r"[\\/]EFI[\\/]BOOT[\\/]BOOT[^\\/]*\.EFI$", loader or "", re.IGNORECASE))


def parse_efibootmgr():
    out = run(["efibootmgr", "-v"])
    entries = []
    boot_order = []
    timeout = None
    for line in out.splitlines():
        if line.startswith("BootOrder:"):
            boot_order = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
            continue
        if line.startswith("Timeout:"):
            m_timeout = re.match(r"Timeout:\s*(\d+)\s*seconds?", line)
            if m_timeout:
                timeout = int(m_timeout.group(1))
            continue
        m = re.match(r"^Boot([0-9A-Fa-f]{4})(\*)?\s+(.*?)\t(.*)", line)
        if not m:
            continue
        num, active, label, devpath = m.groups()
        fp = re.search(r"File\(([^)]+)\)", devpath or "")
        gp = re.search(r"GPT,([0-9a-f-]+)", devpath or "", re.IGNORECASE)
        parts = re.split(r"File\([^)]+\)", devpath or "")
        optional_data = _decode_optional_data(parts[-1]).strip() if len(parts) > 1 else ""
        entries.append(
            {
                "num": num,
                "active": active == "*",
                "label": label.strip(),
                "file": fp.group(1) if fp else "",
                "gpt_uuid": gp.group(1).lower() if gp else "",
                "options": optional_data,
            }
        )
    return entries, boot_order, timeout


def detect_esp_disks():
    """Return (esp_disks, total_slots).

    esp_disks: currently-present ESP partitions, each a dict with a stable
    `index` (the mdadm RaidDevice role, 0 on single-disk hosts), `disk`,
    `part`, `gpt_uuid`.
    total_slots: the mirror's configured member count (1 when not mdadm-backed)
    — the index range a fully-populated mirror spans, used to recognise a
    currently-absent-but-valid "(disk N)" entry as one of ours.
    """
    disks = []
    if os.path.exists("/dev/md/efi"):
        # --export emits machine-stable MD_DEVICES (configured count) plus
        # MD_DEVICE_<x>_ROLE / _DEV pairs; ROLE is the RaidDevice number for an
        # active member and a word (spare/faulty) otherwise. Keying on ROLE
        # gives each disk a slot index that survives a sibling failing.
        export = run(["mdadm", "--detail", "--export", "/dev/md/efi"])
        members = {}
        total_slots = 0
        for line in export.splitlines():
            m = re.match(r"MD_DEVICES=(\d+)$", line)
            if m:
                total_slots = int(m.group(1))
                continue
            m = re.match(r"MD_DEVICE_(\w+)_(DEV|ROLE)=(.*)$", line)
            if m:
                members.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
        for info in members.values():
            role = info.get("ROLE", "")
            if role.isdigit() and info.get("DEV"):
                _add_disk(disks, info["DEV"], int(role))
        total_slots = max(total_slots, len(disks), 1)
    else:
        source = run(["findmnt", "-n", "-e", "-o", "SOURCE", "/boot/efi"])
        _add_disk(disks, source, 0)
        total_slots = 1
    if not disks:
        print("No ESP disks detected", file=sys.stderr)
        sys.exit(1)
    disks.sort(key=lambda d: d["index"])
    return disks, total_slots


def _add_disk(disks, part, index):
    partname = os.path.basename(part)
    info = json.loads(run(["lsblk", "-J", "-n", "-o", "PKNAME,PARTUUID", part]))
    blockdevices = info.get("blockdevices", [])
    if len(blockdevices) != 1:
        raise RuntimeError(f"lsblk {part}: expected exactly one blockdevice, got {len(blockdevices)}")
    dev = blockdevices[0]
    if not dev.get("pkname"):
        raise RuntimeError(f"lsblk {part}: no parent disk (pkname) — unexpected device stacking")
    if not dev.get("partuuid"):
        raise RuntimeError(f"lsblk {part}: no partuuid")
    # The partition number is read from sysfs rather than lsblk's PARTN column:
    # PARTN landed in util-linux 2.38 and the fleet's jammy ships 2.37.2.
    partnum = Path(f"/sys/class/block/{partname}/partition").read_text().strip()
    disks.append({"index": index, "disk": f"/dev/{dev['pkname']}", "part": int(partnum), "gpt_uuid": dev["partuuid"].lower()})


def expand_entries(desired, esp_disks, total_slots):
    multi = total_slots > 1
    expanded = []
    for entry in desired:
        if entry.get("multi_disk") and multi:
            for disk in esp_disks:
                expanded.append(
                    {
                        "label": f"{entry['label']} (disk {disk['index']})",
                        "loader": entry["loader"],
                        "options": entry.get("options", ""),
                        "disk": disk["disk"],
                        "part": disk["part"],
                        "gpt_uuid": disk["gpt_uuid"],
                        "match_disk": True,
                    }
                )
        else:
            expanded.append(
                {
                    "label": entry["label"],
                    "loader": entry["loader"],
                    "options": entry.get("options", ""),
                    "disk": esp_disks[0]["disk"],
                    "part": esp_disks[0]["part"],
                    "gpt_uuid": esp_disks[0]["gpt_uuid"],
                    "match_disk": False,
                }
            )
    return expanded


def all_managed_labels(desired, total_slots):
    labels = set()
    for entry in desired:
        labels.add(entry["label"].lower())
        if entry.get("multi_disk"):
            for idx in range(total_slots):
                labels.add(f"{entry['label']} (disk {idx})".lower())
    return labels


def _is_absent_slot_entry(ce, desired, present_indices, total_slots):
    """A "(disk N)" entry for a configured mirror slot that isn't currently
    present (a failed/removed member) — keep it, so the survivor's own entry
    isn't deleted while it's offline."""
    for entry in desired:
        if not entry.get("multi_disk"):
            continue
        m = re.match(rf"^{re.escape(entry['label'])} \(disk (\d+)\)$", ce["label"], re.IGNORECASE)
        if not m:
            continue
        n = int(m.group(1))
        if n < total_slots and n not in present_indices and loader_eq(ce["file"], entry["loader"]):
            return True
    return False


def loader_eq(a, b):
    if not a or not b:
        return False
    return _norm_loader(a) == _norm_loader(b)


def _validate_desired(desired):
    # Labels/loaders/options become efibootmgr argv (-L/-l/--unicode). argv form
    # already blocks shell injection, but a value starting with '-' can be parsed
    # as a flag, and a control char can corrupt the entry — reject both loudly.
    for entry in desired:
        for key in ("label", "loader", "options"):
            val = entry.get(key, "")
            if not val:
                continue
            if val.startswith("-"):
                raise ValueError(f"invalid {key} {val!r}: must not start with '-'")
            if any(ord(c) < 0x20 for c in val):
                raise ValueError(f"invalid {key} {val!r}: must not contain control characters")


def main():
    desired = json.loads(os.environ["BOOT_EFI_ENTRIES"])
    desired_timeout = json.loads(os.environ.get("BOOT_EFI_TIMEOUT", "null"))
    check = "--check" in sys.argv

    _validate_desired(desired)

    current, boot_order, current_timeout = parse_efibootmgr()
    esp_disks, total_slots = detect_esp_disks()
    expanded = expand_entries(desired, esp_disks, total_slots)
    managed = all_managed_labels(desired, total_slots)
    present_indices = {d["index"] for d in esp_disks}

    exp_labels = [de["label"] for de in expanded]
    if len(exp_labels) != len(set(exp_labels)):
        raise ValueError(f"duplicate EFI entry labels after expansion: {sorted(exp_labels)}")

    actions = []

    # --- Match desired entries to current entries ---
    matched = set()  # current indices consumed by a match
    matched_de = set()  # expanded indices that found a match
    matched_loaders = set()  # normalized loaders covered by a matched managed entry
    for di, de in enumerate(expanded):
        for ci, ce in enumerate(current):
            if ci in matched:
                continue
            if ce["label"] != de["label"]:
                continue
            if not loader_eq(ce["file"], de["loader"]):
                continue
            if de["match_disk"] and ce["gpt_uuid"] and de["gpt_uuid"]:
                if ce["gpt_uuid"] != de["gpt_uuid"]:
                    continue
            if de["options"] and _norm_options(ce["options"]) != _norm_options(de["options"]):
                continue
            matched.add(ci)
            matched_de.add(di)
            matched_loaders.add(_norm_loader(de["loader"]))
            break

    # --- Entries to remove ---
    to_remove = []
    for ci, ce in enumerate(current):
        if ci in matched:
            continue
        if _is_absent_slot_entry(ce, desired, present_indices, total_slots):
            continue
        if ce["label"].lower() in managed:
            to_remove.append(ce)
            actions.append(f"remove '{ce['label']}' (Boot{ce['num']})")
            continue
        # A non-managed entry whose loader a *matched* managed entry already
        # covers is a firmware-recreated duplicate (e.g. a "rEFInd Boot Manager"
        # the firmware re-adds pointing at the same binary as managed "rEFInd").
        # If no managed entry matched that loader, keep it — it may be the only
        # path to that binary, and removing it could orphan the boot chain.
        if _is_removable_fallback(ce["file"]):
            continue
        if _norm_loader(ce["file"]) in matched_loaders:
            to_remove.append(ce)
            actions.append(f"remove '{ce['label']}' (Boot{ce['num']}, stale label)")

    # --- Entries to create ---
    to_create = [de for di, de in enumerate(expanded) if di not in matched_de]
    for de in to_create:
        actions.append(f"create '{de['label']}' -> {de['loader']} " f"on {de['disk']} part {de['part']}")

    # --- Apply (create first, then remove — a failed create can't orphan the boot chain) ---
    if not check:
        for de in to_create:
            cmd = [
                "efibootmgr",
                "-q",
                "-c",
                "-d",
                de["disk"],
                "-p",
                str(de["part"]),
                "-L",
                de["label"],
                "-l",
                de["loader"],
            ]
            if de.get("options"):
                cmd.extend(["--unicode", de["options"]])
            subprocess.run(cmd, capture_output=True, check=True, text=True, timeout=CMD_TIMEOUT)
        for ce in to_remove:
            subprocess.run(
                ["efibootmgr", "-q", "-b", ce["num"], "-B"],
                capture_output=True,
                check=True,
                text=True,
                timeout=CMD_TIMEOUT,
            )

    # --- BootOrder ---
    if not check and (to_remove or to_create):
        current, boot_order, current_timeout = parse_efibootmgr()
        # Safety: never reorder onto an empty/half-applied NVRAM. Confirm at least
        # one entry survives and every created entry actually landed, so a silent
        # firmware GC or a failed create surfaces as a loud failure with the old
        # entries still present rather than a bricked boot.
        if not current:
            raise RuntimeError("refusing to reorder: NVRAM has no boot entries after converge")
        for de in to_create:
            if not any(ce["label"] == de["label"] and loader_eq(ce["file"], de["loader"]) for ce in current):
                raise RuntimeError(f"created entry '{de['label']}' did not appear in NVRAM after efibootmgr -c")

    desired_order = []
    for de in expanded:
        for ce in current:
            if ce["label"] == de["label"] and loader_eq(ce["file"], de["loader"]):
                if ce["num"] not in desired_order:
                    desired_order.append(ce["num"])
                break
    for num in boot_order:
        if num not in desired_order:
            desired_order.append(num)
    for ce in current:
        if ce["num"] not in desired_order:
            desired_order.append(ce["num"])

    if desired_order != boot_order:
        reorder_msg = f"reorder {','.join(boot_order)} -> {','.join(desired_order)}"
        if check and (to_create or to_remove):
            reorder_msg += " (approximate, entries would be created/removed first)"
        actions.append(reorder_msg)
        if not check:
            subprocess.run(
                ["efibootmgr", "-q", "-o", ",".join(desired_order)],
                capture_output=True,
                check=True,
                text=True,
                timeout=CMD_TIMEOUT,
            )

    # --- Timeout ---
    if desired_timeout is not None and current_timeout != desired_timeout:
        actions.append(f"timeout {current_timeout} -> {desired_timeout}")
        if not check:
            subprocess.run(
                ["efibootmgr", "-q", "-t", str(desired_timeout)],
                capture_output=True,
                check=True,
                text=True,
                timeout=CMD_TIMEOUT,
            )

    json.dump({"changed": bool(actions), "actions": actions}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        msg = f"{e.cmd!r} returned {e.returncode}"
        if e.stderr:
            msg += f": {e.stderr.strip()}"
        json.dump({"changed": False, "actions": [], "failed": True, "msg": msg}, sys.stdout)
        sys.exit(1)
    except subprocess.TimeoutExpired as e:
        json.dump(
            {"changed": False, "actions": [], "failed": True, "msg": f"{e.cmd!r} timed out after {e.timeout}s"},
            sys.stdout,
        )
        sys.exit(1)
    except Exception as e:
        json.dump({"changed": False, "actions": [], "failed": True, "msg": str(e)}, sys.stdout)
        sys.exit(1)
