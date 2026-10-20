"""Check the ha:history recorder query helpers."""

import argparse
from datetime import timedelta

import pytest
from conftest import load_repo_module

history = load_repo_module("mise-tasks/ha/history.py", name="ha_history")


def test_parse_duration_accepts_minutes_hours_and_days() -> None:
    assert history.parse_duration("45m") == timedelta(minutes=45)
    assert history.parse_duration("2h") == timedelta(hours=2)
    assert history.parse_duration("3d") == timedelta(days=3)


@pytest.mark.parametrize("text", ["", "h", "2", "2w", "-2h", "1.5h"])
def test_parse_duration_rejects_other_forms(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        history.parse_duration(text)


def test_match_entities_globs_live_ids_and_keeps_unknown_exact_ids() -> None:
    live = ["climate.a", "climate.b", "sensor.a_temperature"]

    assert history.match_entities("climate.*", live) == ["climate.a", "climate.b"]
    # The recorder still holds history for entities removed since.
    assert history.match_entities("sensor.removed", live) == ["sensor.removed"]
    assert history.match_entities("light.*", live) == []


def test_referencing_configs_finds_automations_and_scripts(tmp_path) -> None:
    (tmp_path / "automations.yaml").write_text(
        "- id: '1'\n"
        "  alias: Heat\n"
        "  triggers: [{trigger: state, entity_id: sensor.temp}]\n"
        "  actions: [{action: climate.set_hvac_mode, target: {entity_id: climate.room}}]\n"
        "- id: '2'\n"
        "  alias: Blueprint\n"
        "  use_blueprint: {path: x.yaml, input: {sensor: !secret other}}\n"
    )
    (tmp_path / "scripts.yaml").write_text("boost:\n  sequence: [{action: climate.turn_on, entity_id: climate.room}]\n")

    references = history.referencing_configs(tmp_path, ["climate.room", "sensor.temp", "light.x"])

    assert references == {
        "climate.room": ["automation 'Heat' (id 1)", "script.boost"],
        "sensor.temp": ["automation 'Heat' (id 1)"],
        "light.x": [],
    }


def test_format_history_collapses_attribute_only_updates() -> None:
    rows = [
        [
            {"entity_id": "climate.room", "state": "off", "last_changed": "2026-01-01T10:00:00+00:00"},
            {"state": "off", "last_changed": "2026-01-01T10:05:00+00:00"},
            {"state": "heat", "last_changed": "2026-01-01T10:10:00+00:00"},
        ],
        [],
    ]

    lines = history.format_history(rows, attributes=False)

    assert lines[0] == "climate.room"
    assert [line.split()[-1] for line in lines[1:]] == ["off", "heat"]
