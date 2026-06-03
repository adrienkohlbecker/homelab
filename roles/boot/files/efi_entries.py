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


def run(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def parse_efibootmgr():
    out = run(["efibootmgr", "-v"])
    entries = []
    boot_order = []
    timeout = None
    for line in out.splitlines():
        if line.startswith("BootOrder:"):
            boot_order = [x.strip() for x in line.split(":", 1)[1].split(",")]
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
        entries.append(
            {
                "num": num,
                "active": active == "*",
                "label": label.strip(),
                "file": fp.group(1) if fp else "",
                "gpt_uuid": gp.group(1).lower() if gp else "",
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
    pkname = run(["lsblk", "-n", "-o", "PKNAME", part])
    partnum = open(f"/sys/class/block/{partname}/partition").read().strip()
    gpt_uuid = run(["blkid", "-s", "PARTUUID", "-o", "value", part]).lower()
    disks.append({"disk": f"/dev/{pkname}", "part": int(partnum), "gpt_uuid": gpt_uuid})


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
            matched.add(idx)
            break

    # --- Entries to remove ---
    to_remove = []
    for idx, ce in enumerate(current):
        if idx in matched:
            continue
        if ce["label"].lower() in managed:
            to_remove.append(ce)
            actions.append(f"remove '{ce['label']}' (Boot{ce['num']})")
            continue
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

    # --- Apply ---
    if not check:
        for ce in to_remove:
            subprocess.check_call(
                ["efibootmgr", "-b", ce["num"], "-B"],
                stdout=subprocess.DEVNULL,
            )
        for de in to_create:
            cmd = [
                "efibootmgr",
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
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL)

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
        actions.append(f"reorder {','.join(boot_order)} -> {','.join(desired_order)}")
        if not check:
            subprocess.check_call(
                ["efibootmgr", "-o", ",".join(desired_order)],
                stdout=subprocess.DEVNULL,
            )

    # --- Timeout ---
    if desired_timeout is not None and current_timeout != desired_timeout:
        actions.append(f"timeout {current_timeout} -> {desired_timeout}")
        if not check:
            subprocess.check_call(
                ["efibootmgr", "-t", str(desired_timeout)],
                stdout=subprocess.DEVNULL,
            )

    json.dump({"changed": bool(actions), "actions": actions}, sys.stdout)


if __name__ == "__main__":
    main()
