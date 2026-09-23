#!/usr/bin/env python3
"""Apply the coordinator callback fix to the known SILAM Pollen source.

HACS owns this file. Match the affected code exactly so an unfamiliar upstream
revision fails visibly instead of receiving a potentially incompatible edit.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

IMPORT_OLD = "from homeassistant.components.weather import WeatherEntity\n"
IMPORT_NEW = IMPORT_OLD + "from homeassistant.core import callback\n"

CALLBACK_OLD = '''    def _update_listener(self) -> None:  # noqa: D401 (simple name)
        """Запускает асинхронное обновление при каждом refresh координатора."""
        self.hass.async_create_task(self._handle_coordinator_update())
        return None  # явно — Coordinator ожидает None

    async def async_added_to_hass(self) -> None:
        """Настраивает подписку на координатор и делает первое обновление."""
        await super().async_added_to_hass()
        if hasattr(self, "_unsub_coordinator_listener") and self._unsub_coordinator_listener:
            self._unsub_coordinator_listener()
            self._unsub_coordinator_listener = None
        self.async_on_remove(
            self.coordinator.async_add_listener(self._update_listener)
        )
        await self._handle_coordinator_update()

    async def _handle_coordinator_update(self) -> None:
'''

CALLBACK_NEW = '''    async def async_added_to_hass(self) -> None:
        """Initialize forecast state after the coordinator subscription."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
'''


def patched_source(source: str) -> str:
    """Return the fixed source, preserving every unrelated line."""
    if source.count(IMPORT_NEW) == 1 and source.count(CALLBACK_NEW) == 1 and CALLBACK_OLD not in source:
        return source

    if (
        source.count(IMPORT_OLD) != 1
        or IMPORT_NEW in source
        or source.count(CALLBACK_OLD) != 1
        or CALLBACK_NEW in source
    ):
        raise ValueError("unknown SILAM Pollen callback; review upstream before patching")

    updated = source.replace(IMPORT_OLD, IMPORT_NEW, 1).replace(CALLBACK_OLD, CALLBACK_NEW, 1)
    compile(updated, "pollen_forecast.py", "exec")
    return updated


def write_atomically(path: Path, expected: str, source: str) -> None:
    """Keep an Ansible-style backup and preserve ownership and permissions."""
    if path.read_text(encoding="utf-8") != expected:
        raise RuntimeError(f"{path} changed while preparing the patch")
    original = path.stat()
    backup = path.with_name(f"{path.name}.{os.getpid()}.{datetime.now().strftime('%Y-%m-%d@%H:%M:%S')}~")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(source)
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        os.chown(temporary, original.st_uid, original.st_gid)
        if path.stat().st_mtime_ns != original.st_mtime_ns:
            raise RuntimeError(f"{path} changed while preparing the patch")
        os.link(path, backup)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    source = args.path.read_text(encoding="utf-8")
    updated = patched_source(source)
    changed = updated != source
    if changed and not args.check:
        write_atomically(args.path, source, updated)
    print(json.dumps({"changed": changed}))


if __name__ == "__main__":
    main()
