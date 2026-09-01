import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROLE_MAIN = Path(__file__).parent.parent / "roles/lm_sensors/tasks/main.yml"


def _validators() -> list[str]:
    tasks = yaml.safe_load(ROLE_MAIN.read_text())
    return [task["template"]["validate"] for task in tasks if "validate" in task.get("template", {})]


@pytest.mark.parametrize(
    ("sensors_rc", "sensors_stderr", "expected_rc"),
    [
        (0, "", 0),
        (0, "Error: Line 1: Parse error in chip name\n", 1),
        (1, "No sensors found!\n", 0),
        (1, "No sensors found!\nError: Line 2: Parse error\n", 1),
        (2, "unexpected failure\n", 2),
    ],
)
def test_validators_handle_parser_results(
    tmp_path: Path, sensors_rc: int, sensors_stderr: str, expected_rc: int
) -> None:
    validators = _validators()
    assert len(validators) == 1

    sensor = tmp_path / "sensors"
    sensor.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "sys.stderr.write(os.environ['SENSORS_STDERR'])\n"
        "raise SystemExit(int(os.environ['SENSORS_RC']))\n"
    )
    sensor.chmod(0o755)
    config = tmp_path / "config with spaces.conf"
    config.write_text('chip "verify-isa-0000"\n')

    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SENSORS_RC": str(sensors_rc),
        "SENSORS_STDERR": sensors_stderr,
    }
    command = validators[0].replace("%s", shlex.quote(str(config)))
    result = subprocess.run(command, shell=True, env=environment, capture_output=True, text=True)

    assert result.returncode == expected_rc
    assert result.stderr == sensors_stderr
