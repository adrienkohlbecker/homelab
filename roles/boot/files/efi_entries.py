#!/usr/bin/env python3
"""Manage EFI NVRAM boot entries declaratively.

Reads desired entries from the BOOT_EFI_ENTRIES environment variable
(JSON list), compares with current NVRAM state via efibootmgr, and
converges: creates missing entries, removes stale/relabeled managed
entries, and reorders BootOrder so managed entries come first.

Entries with "multi_disk": true are expanded into per-disk variants on
hosts whose ESP is backed by an mdadm RAID1 mirror (/dev/md/efi).

Pass --check to report what would change without modifying NVRAM.

Output: single-line JSON to stdout: {"changed": bool, "actions": [str]}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run(cmd):
    return subprocess.run(cmd, capture_output=True, check=True, text=True).stdout.strip()


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
        optional_data = parts[-1].strip() if len(parts) > 1 else ""
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
    disks = []
    if os.path.exists("/dev/md/efi"):
        detail = run(["mdadm", "--detail", "/dev/md/efi"])
        for line in detail.splitlines():
            if "active sync" in line:
                _add_disk(disks, line.split()[-1])
    else:
        source = run(["findmnt", "-n", "-o", "SOURCE", "/boot/efi"])
        _add_disk(disks, source)
    if not disks:
        print("No ESP disks detected", file=sys.stderr)
        sys.exit(1)
    return disks


def _add_disk(disks, part):
    partname = os.path.basename(part)
    info = json.loads(run(["lsblk", "-J", "-n", "-o", "PKNAME,PARTUUID", part]))
    dev = info["blockdevices"][0]
    partnum = Path(f"/sys/class/block/{partname}/partition").read_text().strip()
    disks.append({"disk": f"/dev/{dev['pkname']}", "part": int(partnum), "gpt_uuid": dev["partuuid"].lower()})


def expand_entries(desired, esp_disks):
    multi = len(esp_disks) > 1
    expanded = []
    for entry in desired:
        if entry.get("multi_disk") and multi:
            for idx, disk in enumerate(esp_disks):
                expanded.append(
                    {
                        "label": f"{entry['label']} (disk {idx})",
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


def all_managed_labels(desired, esp_disks):
    labels = set()
    for entry in desired:
        labels.add(entry["label"].lower())
        if entry.get("multi_disk"):
            for idx in range(len(esp_disks)):
                labels.add(f"{entry['label']} (disk {idx})".lower())
    return labels


def _is_degraded_disk_entry(label, desired, num_active_disks):
    """A (disk N) entry for a disk beyond the current active set — keep it."""
    for entry in desired:
        if not entry.get("multi_disk"):
            continue
        m = re.match(rf"^{re.escape(entry['label'])} \(disk (\d+)\)$", label, re.IGNORECASE)
        if m and int(m.group(1)) >= num_active_disks:
            return True
    return False


def loader_eq(a, b):
    if not a or not b:
        return False
    return a.replace("/", "\\").lower() == b.replace("/", "\\").lower()


def main():
    desired = json.loads(os.environ["BOOT_EFI_ENTRIES"])
    desired_timeout = json.loads(os.environ.get("BOOT_EFI_TIMEOUT", "null"))
    check = "--check" in sys.argv

    current, boot_order, current_timeout = parse_efibootmgr()
    esp_disks = detect_esp_disks()
    expanded = expand_entries(desired, esp_disks)
    managed = all_managed_labels(desired, esp_disks)

    actions = []

    # --- Match current entries to desired entries ---
    matched = set()
    for de in expanded:
        for idx, ce in enumerate(current):
            if idx in matched:
                continue
            if ce["label"] != de["label"]:
                continue
            if not loader_eq(ce["file"], de["loader"]):
                continue
            if de["match_disk"] and ce["gpt_uuid"] and de["gpt_uuid"]:
                if ce["gpt_uuid"] != de["gpt_uuid"]:
                    continue
            if de["options"] and ce["options"] and de["options"] != ce["options"]:
                continue
            matched.add(idx)
            break

    # --- Entries to remove ---
    to_remove = []
    for idx, ce in enumerate(current):
        if idx in matched:
            continue
        if _is_degraded_disk_entry(ce["label"], desired, len(esp_disks)):
            continue
        if ce["label"].lower() in managed:
            to_remove.append(ce)
            actions.append(f"remove '{ce['label']}' (Boot{ce['num']})")
            continue
        # Entries not in managed but sharing a loader path with a desired entry
        # are treated as stale (e.g. firmware-created "rEFInd Boot Manager"
        # pointing to the same EFI binary as the managed "rEFInd" entry).
        for de in expanded:
            if loader_eq(ce["file"], de["loader"]):
                to_remove.append(ce)
                actions.append(f"remove '{ce['label']}' (Boot{ce['num']}, stale label)")
                break

    # --- Entries to create ---
    matched_labels = {current[idx]["label"] for idx in matched}
    to_create = []
    for de in expanded:
        if de["label"] not in matched_labels:
            to_create.append(de)
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
            subprocess.run(cmd, capture_output=True, check=True, text=True)
        for ce in to_remove:
            subprocess.run(
                ["efibootmgr", "-q", "-b", ce["num"], "-B"],
                capture_output=True,
                check=True,
                text=True,
            )

    # --- BootOrder ---
    if not check and (to_remove or to_create):
        current, boot_order, current_timeout = parse_efibootmgr()

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
    except Exception as e:
        json.dump({"changed": False, "actions": [], "failed": True, "msg": str(e)}, sys.stdout)
        sys.exit(1)
