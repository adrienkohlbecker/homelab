#!/usr/bin/env python3
"""Open the local normalized JSONL log store in lnav."""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

LNAV = "/usr/local/bin/lnav"
STORE = Path("/var/log/fluent-bit/lnav.jsonl")
UNIT_SUFFIXES = (
    ".automount",
    ".device",
    ".mount",
    ".path",
    ".scope",
    ".service",
    ".slice",
    ".socket",
    ".swap",
    ".target",
    ".timer",
)


def fail(message: str) -> None:
    print(f"lnav-journal: {message}", file=sys.stderr)
    raise SystemExit(2)


class TimeBound(NamedTuple):
    """A lnav time argument and its epoch, or None for opaque native syntax."""

    value: str
    epoch: float | None


def parse_args(
    argv: list[str], now: datetime | None = None
) -> tuple[TimeBound | None, TimeBound | None, bool, list[str], list[str]]:
    now = now or datetime.now().astimezone()
    since = None
    until = None
    include_all = False
    units: list[str] = []
    lnav_args: list[str] = []
    index = 0

    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            lnav_args.extend(argv[index + 1 :])
            break
        option, separator, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if option in ("-S", "--since", "-U", "--until", "-u", "--unit"):
            if not separator:
                index += 1
                if index == len(argv):
                    fail(f"{arg} requires a value")
                value = argv[index]
            if option in ("-u", "--unit"):
                units.append(value)
            else:
                bound = parse_bound(value, now)
                if option in ("-S", "--since"):
                    since = bound
                else:
                    until = bound
                lnav_args.extend([f"{option}={bound.value}"] if separator else [option, bound.value])
        elif arg == "--all":
            include_all = True
        elif arg == "--lines" or arg.startswith("--lines="):
            fail("--lines is not supported; use --since/--until to bound the JSONL store")
        elif arg == "-n" and index + 1 < len(argv) and argv[index + 1].isdigit():
            fail("-n <count> is not supported; bare -n still enables lnav headless mode")
        else:
            lnav_args.append(arg)
        index += 1

    return since, until, include_all, units, lnav_args


def discover_logs(store: Path = STORE) -> list[Path]:
    candidates: list[tuple[int, Path]] = []
    pattern = re.compile(rf"{re.escape(store.name)}\.(\d+)(?:\.gz)?$")

    if store.is_file() and os.access(store, os.R_OK):
        candidates.append((0, store))
    for path in store.parent.glob(f"{store.name}.*"):
        match = pattern.fullmatch(path.name)
        if match and path.is_file() and os.access(path, os.R_OK):
            candidates.append((int(match.group(1)), path))

    return [path for _, path in sorted(candidates, key=lambda item: item[0])]


def parse_timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.timestamp()


def first_timestamp(path: Path) -> float | None:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    return parse_timestamp(json.loads(line)["time"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Unknown file bounds keep the file eligible for lnav inspection.
        return None
    return None


def parse_bound(value: str, now: datetime) -> TimeBound:
    """Resolve supported bounds once; leave native-only syntax to lnav."""
    text = value.strip().lower()
    relative = re.fullmatch(r"(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?)\s+ago", text)
    if relative:
        parsed = now - timedelta(**{relative.group(2).rstrip("s") + "s": int(relative.group(1))})
    elif text == "now":
        parsed = now
    elif text in ("today", "yesterday"):
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        parsed = midnight - timedelta(days=text == "yesterday")
    else:
        try:
            return TimeBound(value, parse_timestamp(value))
        except ValueError:
            # Native lnav syntax stays opaque, disabling file preselection.
            return TimeBound(value, None)

    # lnav requires second precision; file selection must use the same cutoff.
    parsed = parsed.replace(microsecond=0)
    return TimeBound(parsed.isoformat(timespec="seconds"), parsed.timestamp())


def select_logs(
    logs: list[Path],
    since: TimeBound | None,
    until: TimeBound | None,
    include_all: bool,
) -> list[Path]:
    if include_all or (since is None and until is None):
        return logs if include_all else logs[:1]

    since_epoch = since.epoch if since is not None else float("-inf")
    until_epoch = until.epoch if until is not None else float("inf")
    if since_epoch is None or until_epoch is None:
        return logs

    selected = []
    for index, path in enumerate(logs):
        start = first_timestamp(path)
        if start is None or (start <= until_epoch and path.stat().st_mtime >= since_epoch):
            selected.append(path)
        elif index == 0 and until is None:
            # Keep the active file open so a future --since bound can be followed.
            selected.append(path)
    return selected


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def unit_filter(units: list[str]) -> str:
    expressions = []
    for unit in units:
        if any(character in unit for character in "*?["):
            expressions.append(f":unit GLOB {sql_quote(unit)}")
        else:
            normalized = unit if unit.endswith(UNIT_SUFFIXES) else f"{unit}.service"
            expressions.append(f":unit = {sql_quote(normalized)}")
    return " OR ".join(expressions)


def main(argv: list[str]) -> None:
    if argv[:1] and argv[0] in ("-V", "--version", "-h", "--help"):
        os.execv(LNAV, [LNAV, *argv])

    since, until, include_all, units, lnav_args = parse_args(argv)
    logs = discover_logs()
    if not logs or logs[0] != STORE:
        fail(f"normalized store is missing or unreadable: {STORE}")

    logs = select_logs(logs, since, until, include_all)
    if not logs:
        fail("no retained JSONL files overlap the requested time window")
    if units and any(arg.lstrip().startswith(":filter-expr") for arg in lnav_args):
        fail("-u/--unit cannot be combined with a custom :filter-expr")

    command = [LNAV, "-c", ":set-min-log-level info", "-c", ":goto 100%"]
    if units:
        command.extend(("-c", f":filter-expr {unit_filter(units)}"))
    command.extend(lnav_args)
    command.extend(str(path) for path in logs)
    os.execv(LNAV, command)


if __name__ == "__main__":
    main(sys.argv[1:])
