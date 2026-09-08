#!/usr/bin/env -S uv run --script
# [MISE] description="Read-only audit of the Hetzner Cloud project for unexpected billable resources"
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read-only audit of the homelab Hetzner Cloud project for billable strays.

The token (HCLOUD_TOKEN) is project-scoped, and the project's only intended
standing footprint is:

  - the `fox` server (cpx22) -- the off-home Headscale/DERP VPS (terraform/
    hetzner.tf, notes/headscale_mesh_redesign.md);
  - its two reserved primary IPs (`fox` v4 + `fox_v6`), both attached to fox;
  - the newest couple of `os=ubuntu-zfs` disk snapshots that back fox's boot
    image (published by `mise run packer:hetzner`, pruned to newest-2);
  - free scaffolding: the `hetzner` network, the `fox` firewall, the `laptop`
    SSH key.

Everything else bills and points at a leak: a packer bake server that never
got torn down, a detached volume, an unassigned (still-billed) IPv4 primary IP,
a floating IP, a load balancer, server backups (a +20% surcharge), or snapshots
piling up past the prune horizon. This sweeps the project for all of those.

It NEVER mutates. For each prunable/orphaned snapshot it prints the exact
hcloud delete line for the operator to review and run by hand.

Scope note: HCLOUD_TOKEN only sees Hetzner *Cloud*, and only the one project it
belongs to. Hetzner Robot (dedicated servers, Storage Boxes) is a separate
product behind separate credentials and is NOT covered here -- the homelab uses
none today.

Exposed as ci:audit-hetzner. Exits 1 if any anomaly is found, 0 when clean, so
it can double as a periodic check.
"""

import json
import shutil
import subprocess
import sys

anomalies: list[str] = []  # human-readable lines, one per unexpected resource
deletes: list[str] = []  # suggested cleanup commands (never executed here)
expected: list[str] = []  # legitimate standing infra, reported for context


def hcloud_list(resource: str, *flags: str) -> list:
    """Return the complete JSON list for one hcloud resource."""
    try:
        out = subprocess.run(
            ["hcloud", resource, "list", *flags, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else error
        raise RuntimeError(f"hcloud {resource} list failed: {detail}") from error
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"hcloud {resource} list returned invalid JSON") from error


def preflight() -> list:
    """The hcloud CLI authenticates on its own -- from $HCLOUD_TOKEN (CI/CD
    variable) or the active local context (~/.config/hcloud/cli.toml, e.g.
    `hcloud context create homelab` seeded from 1Password). Fail early with
    guidance if the binary is missing or no credential resolves, rather than
    letting every per-resource query below append an identical auth error."""
    if not shutil.which("hcloud"):
        sys.exit("hcloud CLI not found -- it is pinned in mise.toml [tools]; run `mise install`.")
    try:
        return hcloud_list("server")
    except RuntimeError as error:
        sys.exit(
            "hcloud cannot authenticate -- set HCLOUD_TOKEN or configure a "
            f"context (`hcloud context create`).\n  {error}"
        )


def gb(image) -> str:
    size = image.get("image_size")
    return f"{size:.1f}GB" if isinstance(size, (int, float)) else "?GB"


def main():
    servers = preflight()
    print("== Hetzner Cloud project audit (scope: the project HCLOUD_TOKEN belongs to) ==\n")

    # ── Servers: only fox (cpx22) is expected; backups are a billable surcharge ──
    for s in servers:
        st, status = s["server_type"]["name"], s["status"]
        if s["name"] == "fox":
            expected.append(f"server fox ({st}, {status})")
        else:
            anomalies.append(f"server {s['name']} ({st}, {status}) -- only fox is expected, likely a leaked bake")
        if s.get("backup_window"):
            anomalies.append(f"server {s['name']} has backups enabled (window {s['backup_window']}) -- +20% surcharge")

    # Snapshots backing a *running* server are kept regardless of age, matching
    # packer:hcloud-prune-snapshots so the audit and the prune agree.
    in_use = {(s.get("image") or {}).get("id") for s in servers if s.get("status") == "running"}
    in_use.discard(None)

    # ── Block storage / standalone networking: none expected, all billable ──
    anomalies.extend(
        f"volume {v['name']} ({v['size']}GB, {v.get('status')}) -- billable, none expected"
        for v in hcloud_list("volume")
    )

    anomalies.extend(
        f"floating IP {f['name']} {f.get('ip')} ({f['type']}) -- billable, none expected"
        for f in hcloud_list("floating-ip")
    )

    anomalies.extend(
        f"load balancer {lb['name']} ({lb['load_balancer_type']['name']}) -- billable, none expected"
        for lb in hcloud_list("load-balancer")
    )

    # ── Primary IPs: fox + fox_v6, both assigned. An UNASSIGNED IPv4 still bills ──
    for p in hcloud_list("primary-ip"):
        if p.get("assignee_id"):
            expected.append(f"primary IP {p['name']} {p['ip']} ({p['type']}, assigned)")
        elif p["type"] == "ipv4":
            anomalies.append(f"primary IP {p['name']} {p['ip']} (ipv4, UNASSIGNED) -- still billed while detached")
        else:
            expected.append(f"primary IP {p['name']} {p['ip']} (ipv6, unassigned -- free, but cruft)")

    # ── Snapshots: keep newest-2 of the ubuntu-zfs family + any in-use image; ──
    # everything else is a billable stray the prune normally clears.
    snaps = hcloud_list("image", "--type", "snapshot")
    family = sorted(
        (i for i in snaps if i.get("labels", {}).get("os") == "ubuntu-zfs"),
        key=lambda i: i.get("created", ""),
        reverse=True,
    )
    keep = {i["id"] for i in family[:2]} | in_use
    for i in family:
        desc = f"snapshot {i['id']} ({gb(i)}, {i.get('created', '')[:10]}, {i.get('description', '')!r})"
        if i["id"] in keep:
            expected.append(desc)
        else:
            anomalies.append(f"{desc} -- ubuntu-zfs snapshot past the newest-2 prune horizon")
            deletes.append(f"hcloud image delete {i['id']}")
    for i in (i for i in snaps if i.get("labels", {}).get("os") != "ubuntu-zfs"):
        anomalies.append(
            f"snapshot {i['id']} ({gb(i)}, {i.get('created', '')[:10]}, {i.get('description', '')!r}) -- not an ubuntu-zfs image, unknown source"
        )
        deletes.append(f"hcloud image delete {i['id']}")

    # ── Server backups (type=backup images): billable, none expected ──
    for b in hcloud_list("image", "--type", "backup"):
        src = (b.get("created_from") or {}).get("name", "?")
        anomalies.append(f"server backup {b['id']} ({gb(b)}, of {src}) -- backups billable, none expected")

    # ── Free scaffolding, reported only for context ──
    for label, resource in (
        ("network", "network"),
        ("firewall", "firewall"),
        ("SSH key", "ssh-key"),
    ):
        expected.extend(f"{label} {item['name']} (free)" for item in hcloud_list(resource))

    print("── Expected standing infra ──")
    print("\n".join(f"  {line}" for line in expected) or "  (none)")

    print("\n── Anomalies (billable / unexpected) ──")
    if anomalies:
        print("\n".join(f"  {line}" for line in anomalies))
        if deletes:
            print("\n── Suggested cleanup (review, then run by hand -- NOT executed) ──")
            print("\n".join(f"  {cmd}" for cmd in deletes))
    else:
        print("  none -- project holds only the expected fox infra")

    verdict = len(anomalies)
    print(f"\nVerdict: {verdict} anomal{'y' if verdict == 1 else 'ies'}")
    sys.exit(1 if anomalies else 0)


if __name__ == "__main__":
    main()
