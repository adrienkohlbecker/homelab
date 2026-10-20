#!/usr/bin/env python3
# [MISE] description="Print Home Assistant recorder history for entities matching a glob, plus the automations and scripts that reference them (read-only)"
# [USAGE] arg "<entity>" help="entity_id or fnmatch glob, e.g. 'climate.*' or 'sensor.living_*_temperature'"
# [USAGE] flag "--since <duration>" help="how far back to look: <n>m, <n>h or <n>d" default="2h"
# [USAGE] flag "--attributes" help="include each state's attributes"
"""
Read-only window into Home Assistant's recorder, through the REST API on lab.

The glob is matched against the live entity list; an exact entity_id that no
longer exists is still queried, since the recorder keeps its history. The
GUI config clone is then searched for automations and scripts that mention
each matched entity, so a state change can be traced to what caused it.
"""

import argparse
import fnmatch
import json
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

# Reuse the sibling ha:sync task's host constants and token lookup.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync import CLONE, HA_URL, ha_api_token

UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    """Parse '<n>m', '<n>h' or '<n>d' into a timedelta."""
    amount, unit = text[:-1], text[-1:]
    if unit not in UNITS or not amount.isdigit():
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}; use <n>m, <n>h or <n>d")
    return timedelta(**{UNITS[unit]: int(amount)})


def match_entities(pattern: str, entity_ids: list[str]) -> list[str]:
    """Return the entity_ids matching pattern, or pattern itself when it is an exact id."""
    matched = sorted(fnmatch.filter(entity_ids, pattern))
    if not matched and not any(c in pattern for c in "*?["):
        return [pattern]
    return matched


class _AnyTagLoader(yaml.SafeLoader):
    """SafeLoader that reads HA's custom tags (!input, !secret, ...) as plain values."""


_AnyTagLoader.add_multi_constructor("!", lambda loader, _suffix, node: loader.construct_scalar(node))


def _load_yaml(path: Path) -> Any:
    return yaml.load(path.read_text(), Loader=_AnyTagLoader) if path.exists() else None


def referencing_configs(clone: Path, entity_ids: list[str]) -> dict[str, list[str]]:
    """Map each entity_id to the automations and scripts whose config mentions it."""
    automations = _load_yaml(clone / "automations.yaml") or []
    scripts = _load_yaml(clone / "scripts.yaml") or {}
    configs = [(f"automation {item.get('alias', '?')!r} (id {item.get('id', '?')})", item) for item in automations] + [
        (f"script.{key}", item) for key, item in scripts.items()
    ]

    dumped = [(label, json.dumps(item, default=str)) for label, item in configs]
    return {entity: [label for label, text in dumped if entity in text] for entity in entity_ids}


def ha_get(path: str, token: str) -> Any:
    req = urllib.request.Request(f"{HA_URL}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_history(entity_ids: list[str], start: datetime, end: datetime, token: str, attributes: bool) -> list:
    query = {"filter_entity_id": ",".join(entity_ids), "end_time": end.isoformat()}
    if not attributes:
        query |= {"minimal_response": "", "no_attributes": ""}
    return ha_get(f"/api/history/period/{start.isoformat()}?{urllib.parse.urlencode(query)}", token)


def format_history(history: list, attributes: bool) -> list[str]:
    """Render one block per entity: a header, then one local-time line per state.

    The first row is the state at the window start. Without attributes,
    consecutive rows with the same state (attribute-only updates) collapse.
    """
    lines: list[str] = []
    for states in history:
        if not states:
            continue
        lines.append(states[0]["entity_id"])
        previous = None
        for state in states:
            if not attributes and state["state"] == previous:
                continue
            previous = state["state"]
            changed = datetime.fromisoformat(state["last_changed"]).astimezone()
            line = f"  {changed:%Y-%m-%d %H:%M:%S}  {state['state']}"
            if attributes:
                line += f"  {json.dumps(state.get('attributes', {}), sort_keys=True)}"
            lines.append(line)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Home Assistant recorder history (read-only).")
    parser.add_argument("entity")
    parser.add_argument("--since", type=parse_duration, default=parse_duration("2h"))
    parser.add_argument("--attributes", action="store_true")
    args = parser.parse_args()

    token = ha_api_token()
    entity_ids = match_entities(args.entity, [s["entity_id"] for s in ha_get("/api/states", token)])
    if not entity_ids:
        sys.exit(f"no entity matches {args.entity!r}")

    now = datetime.now(UTC)
    history = fetch_history(entity_ids, now - args.since, now, token, args.attributes)
    print("\n".join(format_history(history, args.attributes)) or "no state changes in the window")

    references = referencing_configs(CLONE, entity_ids)
    print("\nreferenced by (ha_gui_config):")
    for entity, labels in references.items():
        if labels:
            print(f"  {entity}: {', '.join(labels)}")
    if unreferenced := sum(not labels for labels in references.values()):
        print(f"  {unreferenced} of {len(references)} matched entities: no automation or script")


if __name__ == "__main__":
    main()
