"""Check the fan demand produced by the rendered production curves."""

from itertools import pairwise
from pathlib import Path

import jinja2
import pytest
import yaml

TEMPLATES = Path(__file__).resolve().parents[1] / "roles/fan2go/templates"


def _config(host: str) -> dict:
    template = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
        (TEMPLATES / f"{host}.yaml.j2").read_text()
    )
    return yaml.safe_load(
        template.render(
            service_ports={"fan2go_api": 9919},
            rotational_disks_by_id=[f"ata-disk-{index}" for index in range(4 if host == "lab" else 2)],
        )
    )


def _fan_demand(config: dict, fan_id: str, temperatures: dict[str, int]) -> float:
    curves = {curve["id"]: curve for curve in config["curves"]}
    fan = next(fan for fan in config["fans"] if fan["id"] == fan_id)

    def demand(curve_id: str) -> float:
        curve = curves[curve_id]
        if function := curve.get("function"):
            assert function["type"] == "maximum"
            return max(demand(child) for child in function["curves"])

        linear = curve["linear"]
        temperature = temperatures[linear["sensor"]]
        if "steps" in linear:
            points = [
                (int(temp), int(percent.rstrip("%"))) for step in linear["steps"] for temp, percent in step.items()
            ]
        else:
            points = [(linear["min"], 0), (linear["max"], 100)]
        for (low_temp, low_demand), (high_temp, high_demand) in pairwise(points):
            if temperature <= high_temp:
                if temperature <= low_temp:
                    return low_demand
                return low_demand + (temperature - low_temp) * (high_demand - low_demand) / (high_temp - low_temp)
        return points[-1][1]

    return demand(fan["curve"])


@pytest.mark.parametrize(
    ("host", "case_fan", "disk_fan", "case_demand_at_48"),
    [
        ("lab", "cha_fan1_exhaust", "cha_fan2_intake", 40),
        ("pug", "cha_fan_2", "cha_fan_1_hdd", 60),
    ],
)
def test_case_fans_follow_either_disk_without_losing_cpu_response(
    host: str, case_fan: str, disk_fan: str, case_demand_at_48: int
) -> None:
    config = _config(host)
    temperatures = {
        "nct6798_temp2": 35,
        "cpu_package": 55,
        "cpu_package_temp": 55,
    }
    disk_sensors = [sensor["id"] for sensor in config["sensors"] if "disk" in sensor]
    temperatures.update({sensor: 38 for sensor in disk_sensors})
    assert _fan_demand(config, case_fan, temperatures) == 10

    for hot_disk in disk_sensors:
        temperatures[hot_disk] = 48
        assert _fan_demand(config, case_fan, temperatures) == case_demand_at_48
        assert _fan_demand(config, disk_fan, temperatures) == 100
        temperatures[hot_disk] = 53
        assert _fan_demand(config, case_fan, temperatures) == 100
        temperatures[hot_disk] = 38

    temperatures["cpu_package"] = 80
    temperatures["cpu_package_temp"] = 80
    assert _fan_demand(config, case_fan, temperatures) == 100
    if host == "lab":
        temperatures["hdd_1_temp"] = 53
        temperatures["cpu_package"] = 55
        assert _fan_demand(config, "cpu_fan", temperatures) == 10
