"""Guard the HACS callback patch against upstream drift and partial writes."""

import ast
import json
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "roles/homeassistant/files/silam_pollen_callback.py"

UPSTREAM_SOURCE = '''from homeassistant.components.weather import WeatherEntity
from homeassistant.helpers.entity import EntityDescription

class PollenForecastSensor:
    def _update_listener(self) -> None:  # noqa: D401 (simple name)
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
        self.async_write_ha_state()
'''


def run_patch(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_known_upstream_patch_is_atomic_and_idempotent(tmp_path: Path) -> None:
    component = tmp_path / "pollen_forecast.py"
    component.write_text(UPSTREAM_SOURCE)
    component.chmod(0o640)

    check = run_patch(component, "--check")
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout) == {"changed": True}
    assert component.read_text() == UPSTREAM_SOURCE

    apply = run_patch(component)
    assert apply.returncode == 0, apply.stderr
    assert json.loads(apply.stdout) == {"changed": True}
    assert stat.S_IMODE(component.stat().st_mode) == 0o640
    backups = list(tmp_path.glob("pollen_forecast.py.*~"))
    assert len(backups) == 1
    assert backups[0].read_text() == UPSTREAM_SOURCE

    tree = ast.parse(component.read_text())
    entity = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = [node for node in entity.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert [node.name for node in methods] == [
        "async_added_to_hass",
        "_handle_coordinator_update",
    ]
    callback = methods[1]
    assert isinstance(callback, ast.FunctionDef)
    assert len(callback.decorator_list) == 1
    assert isinstance(callback.decorator_list[0], ast.Name)
    assert callback.decorator_list[0].id == "callback"
    assert "self.async_write_ha_state()" in component.read_text()

    again = run_patch(component)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == {"changed": False}
    assert len(list(tmp_path.glob("pollen_forecast.py.*~"))) == 1


def test_unknown_callback_is_left_untouched(tmp_path: Path) -> None:
    component = tmp_path / "pollen_forecast.py"
    unknown = UPSTREAM_SOURCE.replace("await self._handle_coordinator_update()", "pass")
    component.write_text(unknown)

    result = run_patch(component)

    assert result.returncode != 0
    assert "unknown SILAM Pollen callback" in result.stderr
    assert component.read_text() == unknown
    assert not list(tmp_path.glob("pollen_forecast.py.*~"))
