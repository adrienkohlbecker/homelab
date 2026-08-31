"""Exercise native broker logging and failure visibility in the QEMU fixture."""

import json
import stat
import subprocess
import time
from pathlib import Path


def main():
    config = Path("/mnt/services/mosquitto/config/mosquitto.conf")
    data = Path("/mnt/services/mosquitto/data")
    original_config = config.read_text()
    original_mode = stat.S_IMODE(data.stat().st_mode)
    since = f"@{time.time():.6f}"
    expected = {
        "Reloading config.": ("6", "info"),
        "Warning: The retry_interval option is no longer available.": ("4", "warn"),
        "Saving in-memory database to /mosquitto/data//mosquitto.db.": ("6", None),
        "Error saving in-memory database, unable to open /mosquitto/data//mosquitto.db.new for writing.": (
            "6",
            "error",
        ),
        "Error: Permission denied.": ("3", "error"),
    }
    with Path("/var/log/fluent-bit/lnav.jsonl").open() as store:
        store.seek(0, 2)
        offset = store.tell()
        try:
            # Reloading an ignored option produces a real, nonfatal warning.
            config.write_text(original_config + "retry_interval 20\n")
            subprocess.run(["podman", "kill", "--signal", "HUP", "mosquitto"], check=True, timeout=10)
            data.chmod(original_mode & ~0o222)
            subprocess.run(["podman", "kill", "--signal", "USR1", "mosquitto"], check=True, timeout=10)

            deadline = time.monotonic() + 45
            while True:
                journal = subprocess.run(
                    ["journalctl", "--since", since, "-u", "mosquitto.service", "-o", "json", "--no-pager"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                native = {
                    record["MESSAGE"]: record
                    for line in journal.stdout.splitlines()
                    if (record := json.loads(line)).get("_COMM") == "mosquitto"
                }
                store.seek(offset)
                normalized = {}
                for line in store:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # The daemon may still be appending the final JSON line.
                        continue
                    if record.get("service") == "mosquitto":
                        normalized[record["message"]] = record
                missing = set(expected) - native.keys()
                missing.update(
                    message for message, (_, level) in expected.items() if level and message not in normalized
                )
                if not missing:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"Missing broker records: {missing}; journal={native}; lnav={normalized}")
                time.sleep(1)

            for message, (priority, level) in expected.items():
                record = native[message]
                assert record["_TRANSPORT"] == "syslog", record
                assert record["_SYSTEMD_UNIT"] == "mosquitto.service", record
                assert record["SYSLOG_IDENTIFIER"] == "mosquitto", record
                assert record["PRIORITY"] == priority, record
                assert "CONTAINER_TAG" not in record, record
                if level is None:
                    assert message not in normalized, normalized[message]
                else:
                    record = normalized[message]
                    assert record["unit"] == "mosquitto.service", record
                    assert record["level"] == level, record
            print("Native journal attribution, priorities, lnav severity and save filtering passed")
        finally:
            data.chmod(original_mode)
            config.write_text(original_config)
            subprocess.run(["podman", "kill", "--signal", "HUP", "mosquitto"], check=True, timeout=10)


if __name__ == "__main__":
    main()
