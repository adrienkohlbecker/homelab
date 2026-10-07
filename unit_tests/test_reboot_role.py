"""Tests for roles/reboot."""

from pathlib import Path

import jinja2
import yaml


def test_production_reboot_goes_through_logind() -> None:
    task = yaml.safe_load(Path("roles/reboot/tasks/reboot.yml").read_text())[0]
    command = jinja2.Template(task["reboot"]["reboot_command"])

    assert command.render(qemu_test=False) == "/usr/bin/sudo -n /usr/bin/systemctl reboot"
