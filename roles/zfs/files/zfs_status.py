#!/usr/bin/python3
"""Read pool health and scan state without interpreting localized ZFS prose."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any


class StatusError(ValueError):
    """ZFS returned a status document that cannot safely drive monitoring."""


def run(*args: str) -> str:
    """Bound ZFS queries in the C locale, with SIGKILL escalation for a wedged pool."""
    return subprocess.run(
        ["timeout", "-k", "10", "60", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise StatusError(f"Expected a nonnegative integer for {field}: {value!r}")
    return value


def mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatusError(f"Expected an object for {field}")
    return value


def iter_vdevs(devices: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for name, value in devices.items():
        device = mapping(value, name)
        yield name, device
        yield from iter_vdevs(mapping(device.get("vdevs", {}), f"{name}.vdevs"))


def pool_vdevs(pool: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for group in ("vdevs", "dedup", "special", "logs", "l2cache"):
        yield from iter_vdevs(mapping(pool.get(group, {}), group))


def parse_status(raw: str) -> dict[str, Any]:
    """Validate consumed fields while keeping active vdevs and spares separate."""
    document = mapping(json.loads(raw), "status")
    version = mapping(document.get("output_version"), "output_version")
    major = integer(version.get("vers_major"), "vers_major")
    minor = integer(version.get("vers_minor"), "vers_minor")
    if version.get("command") != "zpool status" or major != 0 or minor < 1:
        raise StatusError(f"Unsupported zpool status schema: {version!r}")
    pools = mapping(document.get("pools"), "pools")
    for name, value in pools.items():
        pool = mapping(value, name)
        # Pool state is left to `zpool status -x`, which owns the health verdict.
        if pool.get("name") != name:
            raise StatusError(f"Mismatched name for pool {name}")
        vdevs = mapping(pool.get("vdevs"), f"{name}.vdevs")
        if not vdevs:
            raise StatusError(f"Missing vdevs for pool {name}")
        for device, vdev in pool_vdevs(pool):
            for field in ("read_errors", "write_errors", "checksum_errors"):
                integer(vdev.get(field), f"{device}.{field}")
        for device, value in mapping(pool.get("spares", {}), f"{name}.spares").items():
            mapping(value, device)
        scan = mapping(pool.get("scan_stats", {}), f"{name}.scan_stats")
        # OpenZFS reports error scrubs only in the err_scrub_* fields below.
        if "function" in scan:
            if scan["function"] not in ("NONE", "SCRUB", "RESILVER"):
                raise StatusError(f"Unknown scan function on {name}: {scan['function']!r}")
            if scan.get("state") not in ("NONE", "SCANNING", "FINISHED", "CANCELED"):
                raise StatusError(f"Unknown scan state on {name}: {scan.get('state')!r}")
            for field in ("end_time", "scrub_pause"):
                integer(scan.get(field), f"{name}.{field}")
        elif scan and "rebuild_stats" not in scan:
            raise StatusError(f"Missing scan function on {name}")
        if "err_scrub_state" in scan:
            if scan["err_scrub_state"] not in ("NONE", "SCANNING", "FINISHED", "CANCELED", "ERRORSCRUBBING"):
                raise StatusError(f"Unknown err_scrub_state on {name}: {scan['err_scrub_state']!r}")
            integer(scan.get("err_scrub_pause"), f"{name}.err_scrub_pause")
        for device, value in mapping(scan.get("rebuild_stats", {}), "rebuild_stats").items():
            rebuild = mapping(value, device)
            if rebuild.get("state") not in ("NONE", "ACTIVE", "CANCELED", "COMPLETE"):
                raise StatusError(f"Unknown rebuild state on {device}: {rebuild.get('state')!r}")
            if rebuild["state"] == "COMPLETE":
                integer(rebuild.get("end_time"), f"{device}.end_time")
    return pools


def read_status(*pools: str, explain: bool = False) -> dict[str, Any]:
    # Flat output overwrites an active spare's counters with its spare entry.
    args = ["zpool", "status", "-j", "--json-int", "-p"]
    if explain:
        args.append("-x")
    result = parse_status(run(*args, *pools))
    if pools and (not set(result) <= set(pools) or (not explain and set(result) != set(pools))):
        raise StatusError(f"Requested pools {pools!r}, received {tuple(result)!r}")
    return result


def supports_json() -> bool:
    """Select the parser from the zpool userspace version, not the loaded module.

    Userspace renders `zpool status -j` from the kernel's pool config, so the
    userspace release decides even while an older module is loaded until the
    next reboot. An unrecognized version banner raises rather than guessing.
    """
    version = re.match(r"zfs-(\d+)\.(\d+)\.", run("zpool", "--version"))
    if version is None:
        raise StatusError("Cannot identify the ZFS userspace version")
    return tuple(map(int, version.groups())) >= (2, 3)


def scan_active(pool: dict[str, Any], *, scrub_only: bool = False) -> bool:
    """Return whether the pool is doing scan I/O right now.

    A paused scrub stays SCANNING with a nonzero scrub_pause and counts as
    inactive. scrub_only limits the answer to scrubs the backup window can
    pause with `zpool scrub -p`, excluding resilvers, rebuilds and error scrubs.
    """
    scan = pool.get("scan_stats", {})
    if scan.get("state") == "SCANNING":
        if scan.get("function") == "SCRUB":
            return scan["scrub_pause"] == 0
        if scan.get("function") == "RESILVER" and not scrub_only:
            return True
    if scrub_only:
        return False
    if scan.get("err_scrub_state") == "ERRORSCRUBBING" and scan["err_scrub_pause"] == 0:
        return True
    return any(rebuild["state"] == "ACTIVE" for rebuild in scan.get("rebuild_stats", {}).values())


def scan_in_progress(pool: str | None, *, scrub_only: bool = False) -> bool:
    """Return actual I/O activity; a query failure must never become False."""
    pools = [pool] if pool else []
    if supports_json():
        return any(scan_active(status, scrub_only=scrub_only) for status in read_status(*pools).values())
    # OpenZFS 2.2 has no JSON status; run() pins its prose to the C locale.
    output = run("zpool", "status", *pools)
    return "scrub in progress" in output or (
        not scrub_only and re.search(r"resilver(?: \([^)]*\))? in progress", output) is not None
    )


def scrub_issue(name: str, pool: dict[str, Any], now: int, expire: int) -> str | None:
    """Return the scrub-watchdog alert for one pool, or None.

    A canceled scrub always alerts and an active scan never does. Age runs from
    the newest completed scrub or resilver, completed sequential rebuild, or
    paused scrub. Only when none exists does it query `zfs get creation`, and
    the alert then says the age is measured from pool creation.
    """
    scan = pool.get("scan_stats", {})
    if scan.get("function") == "SCRUB" and scan.get("state") == "CANCELED":
        return f"Last scrub canceled on {name}"
    if scan_active(pool):
        return None
    completed = [
        rebuild["end_time"] for rebuild in scan.get("rebuild_stats", {}).values() if rebuild["state"] == "COMPLETE"
    ]
    if scan.get("function") in ("SCRUB", "RESILVER") and scan.get("state") == "FINISHED":
        completed.append(scan["end_time"])
    elif scan.get("function") == "SCRUB" and scan.get("state") == "SCANNING":
        completed.append(scan["scrub_pause"])
    scrub_date = max(completed) if completed else int(run("zfs", "get", "creation", "-Hpo", "value", name).strip())
    if scrub_date <= 0:
        raise StatusError(f"Missing scrub or creation timestamp for {name}")
    if now - scrub_date >= expire:
        return f"Scrub expired on {name}" + (
            " (age since pool creation; no usable scan timestamp)" if not completed else ""
        )
    return None


def diagnostic(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return f"{error}: {error.stderr.strip()}"
    return str(error)


def finish_health(issues: list[str], report: list[str]) -> int:
    """Print the health report and mail any failures, including startup errors."""
    print("\n".join(report))
    if issues:
        failures = "\n".join(f"ERROR :: {issue}" for issue in issues)
        print(failures, file=sys.stderr)
        subject = f"[{socket.gethostname().split('.')[0]}] zfs health - {len(issues)} issue(s) detected"
        subprocess.run(["mail", "-s", subject, "root"], input="\n".join([failures, *report]), text=True, check=True)
        return 1
    print("Done")
    return 0


def health(expire: int) -> int:
    """Mail counted failures while preserving zpool -x's feature-cap policy."""
    if os.geteuid() != 0:
        raise PermissionError("I require root")
    issues = []
    report = []
    try:
        # SLOW is diagnostic only: its cumulative count is not a daily alarm.
        report.append(run("zpool", "status", "-s"))
    except (OSError, subprocess.CalledProcessError) as error:
        report.append(f"Warning: zpool status report did not complete: {diagnostic(error)}")
    try:
        names = run("zpool", "list", "-H", "-o", "name").splitlines()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        issues.append(f"Cannot list pools: {diagnostic(error)}")
    else:
        now = int(time.time())
        for name in names:
            try:
                if read_status(name, explain=True):
                    issues.append(f"zpool status -x reports a problem: {name}")
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                issues.append(f"Cannot query pool health for {name}: {diagnostic(error)}")
            try:
                pool = read_status(name)[name]
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                issues.append(f"Cannot query drive errors and scrub age for {name}: {diagnostic(error)}")
                continue
            if any(
                vdev.get(field, 0) > 0
                for _, vdev in pool_vdevs(pool)
                for field in ("read_errors", "write_errors", "checksum_errors")
            ):
                issues.append(f"Detected drive errors (READ/WRITE/CKSUM) on {name}")
            for device, spare in pool.get("spares", {}).items():
                if spare.get("state") not in ("AVAIL", "INUSE"):
                    issues.append(f"Unhealthy spare {device} on {name}: {spare.get('state')}")
            try:
                if issue := scrub_issue(name, pool, now, expire):
                    issues.append(issue)
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                issues.append(f"Cannot check scrub age for {name}: {diagnostic(error)}")
    return finish_health(issues, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("health")
    scan = commands.add_parser("scan")
    scan.add_argument("pool", nargs="?")
    scan.add_argument("--scrub-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.command in {None, "health"}:
            try:
                expire = integer(int(os.environ.get("SCRUB_EXPIRE", "3456000")), "SCRUB_EXPIRE")
                json_supported = supports_json()
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                return finish_health([f"Cannot initialize health check: {diagnostic(error)}"], [])
            if json_supported:
                return health(expire)
            try:
                return subprocess.run(["/opt/zfs/zfs_health_legacy.sh"], check=False).returncode
            except OSError as error:
                return finish_health([f"Cannot start legacy health parser: {diagnostic(error)}"], [])
        print(int(scan_in_progress(args.pool, scrub_only=args.scrub_only)))
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"ERROR :: {diagnostic(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
