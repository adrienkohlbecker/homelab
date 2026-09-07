#!/usr/bin/env python3
"""Reconcile Home Assistant's SMTP config entry with the repo's declared inputs.

Home Assistant 2026.9 dropped YAML configuration for the SMTP notify platform.
The live settings are now a config entry inside
``.storage/core.config_entries``, and HA offers no offline API for them, so this
script edits that registry directly.

Two things make that safe enough to do from Ansible:

* The caller stops Home Assistant first. HA holds the registry in memory and
  flushes it on shutdown, so patching a running instance would be undone by the
  very restart meant to pick the patch up. ``--check`` exists so the role can
  find out whether a stop is needed before paying for one.
* The registry's storage version is pinned below. A Home Assistant release that
  bumps it makes this script refuse to write rather than emit an entry in a
  shape the new HA no longer understands.

Identifiers are derived from the sender address rather than randomly generated,
so a registry rebuilt from scratch reuses the ids the entity registry already
references (``<entry_id>_<recipient>``) instead of orphaning every notify
entity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Shape of .storage/core.config_entries this script knows how to write. HA
# migrates older files forward, so refusing only on a *newer* file keeps the
# check loud without breaking on a version we still understand.
STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 5

DOMAIN = "smtp"
EPOCH = "1970-01-01T00:00:00+00:00"


def _stable_id(*parts: str) -> str:
    """Return a deterministic 32-hex config-entry id for the given identity.

    HA generates ULIDs for new entries but only ever treats the id as an opaque
    string; entries created before the ULID switch still carry 32-hex ids.
    """
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def build_entry(args: argparse.Namespace, password: str) -> dict[str, Any]:
    """Build the config entry the declared inputs describe.

    Field-for-field what HA's own SMTP config flow stores, because HA loads the
    registry raw and applies no schema defaults on read.
    """
    entry_id = _stable_id(DOMAIN, args.sender)
    return {
        "created_at": EPOCH,
        "data": {
            "debug": False,
            "encryption": args.encryption,
            "name": args.name,
            "password": password,
            "platform": DOMAIN,
            "port": args.port,
            "recipient": args.recipient,
            "sender": args.sender,
            "sender_name": args.sender_name,
            "server": args.server,
            "username": args.username,
            "verify_ssl": True,
        },
        "disabled_by": None,
        "discovery_keys": {},
        "domain": DOMAIN,
        "entry_id": entry_id,
        "minor_version": 1,
        "modified_at": EPOCH,
        "options": {"timeout": args.timeout},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        # One subentry per recipient; HA derives a notify entity from each and
        # keys it on the subentry's unique_id.
        "subentries": [
            {
                "data": {},
                "subentry_id": _stable_id(DOMAIN, args.sender, recipient),
                "subentry_type": "recipient",
                "title": recipient,
                "unique_id": recipient,
            }
            for recipient in args.recipient
        ],
        # The legacy `notify.<title>` action HA derives from the entry title is
        # what automations call, so the title is a load-bearing input.
        "title": args.name,
        "unique_id": None,
        "version": 1,
    }


def load_registry(path: Path) -> dict[str, Any]:
    """Read the config-entry registry, or return an empty one if HA never ran."""
    if not path.exists():
        return {
            "version": STORAGE_VERSION,
            "minor_version": STORAGE_MINOR_VERSION,
            "key": "core.config_entries",
            "data": {"entries": []},
        }

    registry = json.loads(path.read_text())
    version = (registry["version"], registry["minor_version"])
    if version > (STORAGE_VERSION, STORAGE_MINOR_VERSION):
        raise SystemExit(
            f"{path} is storage version {version[0]}.{version[1]}, newer than the "
            f"{STORAGE_VERSION}.{STORAGE_MINOR_VERSION} this script writes. Re-check "
            "the SMTP entry shape against the running Home Assistant and raise the "
            "pin in smtp_config_entry.py."
        )
    return registry


def reconcile(registry: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Merge the desired entry into the registry. Return whether it changed.

    Only the fields the repo declares are enforced. HA's own bookkeeping --
    creation timestamps and any subentry ids it minted itself -- is carried
    across so reconciling does not churn the entity registry.
    """
    entries: list[dict[str, Any]] = registry["data"]["entries"]
    current = next((e for e in entries if e["domain"] == DOMAIN), None)

    if current is None:
        now = datetime.now(UTC).isoformat()
        entries.append(desired | {"created_at": now, "modified_at": now})
        return True

    merged = desired | {
        "entry_id": current["entry_id"],
        "created_at": current["created_at"],
        "modified_at": current["modified_at"],
        # Discovery bookkeeping and the disabled flag stay HA's (and the
        # operator's) to set. `source` records how the entry first came into
        # being -- HA's own history, not a setting -- so an entry HA imported
        # from the old YAML platform is adopted rather than rewritten, which
        # keeps adoption from costing a restart.
        "discovery_keys": current["discovery_keys"],
        "disabled_by": current["disabled_by"],
        "source": current["source"],
    }
    # Keep the subentry id HA already assigned to a recipient we still want; a
    # fresh id would orphan that recipient's notify entity.
    known = {s["unique_id"]: s["subentry_id"] for s in current["subentries"]}
    for subentry in merged["subentries"]:
        if subentry["unique_id"] in known:
            subentry["subentry_id"] = known[subentry["unique_id"]]

    if merged == current:
        return False

    merged["modified_at"] = datetime.now(UTC).isoformat()
    entries[entries.index(current)] = merged
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--name", required=True, help="entry title; drives notify.<name>")
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--encryption", required=True, choices=["none", "starttls", "tls"])
    parser.add_argument("--sender", required=True)
    parser.add_argument("--sender-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--recipient", action="append", required=True)
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether a write is needed without performing one",
    )
    args = parser.parse_args()

    # Out of band so the password never reaches the host's process table.
    password = os.environ.get("HOMEASSISTANT_SMTP_PASSWORD")
    if not password:
        raise SystemExit("HOMEASSISTANT_SMTP_PASSWORD is unset")

    registry = load_registry(args.storage)
    changed = reconcile(registry, build_entry(args, password))

    if changed and not args.check:
        # Replace via a sibling temp file so a crash mid-write cannot leave HA
        # with a truncated registry and no integrations.
        scratch = args.storage.with_suffix(".ansible-tmp")
        scratch.write_text(json.dumps(registry, indent=2))
        if args.storage.exists():
            scratch.chmod(args.storage.stat().st_mode & 0o777)
            os.chown(scratch, args.storage.stat().st_uid, args.storage.stat().st_gid)
        scratch.replace(args.storage)

    print(json.dumps({"changed": changed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
