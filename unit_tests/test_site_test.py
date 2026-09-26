"""Focused tests for the full-site harness modes."""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import jinja2
import pytest
import site_test
import yaml
from machine import Machine


class SiteTestMachine:
    def __init__(
        self,
        workdir_path: Path,
        *,
        pid1_journal: list[str] | None = None,
        settle_exitcode: int = 0,
    ) -> None:
        self.workdir_path = workdir_path
        self.pid1_journal = pid1_journal or []
        self.settle_exitcode = settle_exitcode
        self.keep_vm = False
        self.ansible_calls: list[tuple[str, ...]] = []
        self.ssh_calls: list[tuple[str, ...]] = []
        self.system_running_calls = 0

    async def __aenter__(self) -> SiteTestMachine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def session(self, timeout: int):
        return Machine.session(cast(Machine, self), timeout)

    async def ensure_booted(self) -> None:
        return None

    async def ensure_ssh(self) -> None:
        return None

    async def ensure_system_running(self) -> None:
        self.system_running_calls += 1

    async def ansible_command(self, *args: str) -> None:
        self.ansible_calls.append(args)

    async def ssh_command(self, *args: str, check: bool = True) -> SimpleNamespace:
        self.ssh_calls.append(args)
        if args[0] == "journalctl" and "_PID=1" in args:
            return SimpleNamespace(exitcode=0, stdout=self.pid1_journal)
        if args[0] == "timeout":
            return SimpleNamespace(exitcode=self.settle_exitcode, stdout=["running"])
        return SimpleNamespace(exitcode=0, stdout=["running"])

    async def wait(self) -> None:
        return None


def test_check_mode_forwards_flag_and_skips_poweroff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = SiteTestMachine(tmp_path)

    asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10, check_mode=True))

    assert machine.ansible_calls == [
        (str(tmp_path / "_environment.yml"),),
        (str(tmp_path / "site.yml"), "-e", "_test_role_under_test=services"),
        (str(tmp_path / "site.yml"), "--check"),
    ]
    assert machine.system_running_calls == 1
    assert machine.ssh_calls == []


def test_converge_poweroff_ignores_fixture_inhibitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = SiteTestMachine(tmp_path)

    asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10))

    assert ("sudo", "systemctl", "--check-inhibitors=no", "poweroff") in machine.ssh_calls


def test_converge_profiles_settled_boot_before_poweroff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = SiteTestMachine(tmp_path)

    asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10))

    assert [call[:2] for call in machine.ssh_calls] == [
        ("timeout", str(site_test.SYSTEM_RUNNING_WAIT_TIMEOUT)),
        ("systemd-analyze", "blame"),
        ("systemd-analyze", "critical-chain"),
        ("journalctl", "--boot"),
        ("sudo", "systemctl"),
    ]


def test_reboot_bypasses_inhibitor_only_in_qemu() -> None:
    task = yaml.safe_load(Path("roles/reboot/tasks/reboot.yml").read_text())[0]
    command = jinja2.Template(task["reboot"]["reboot_command"])

    assert command.render(qemu_test=True) == "/usr/bin/sudo -n /usr/bin/systemctl --check-inhibitors=no reboot"
    assert command.render(qemu_test=False) == "/usr/bin/sudo -n /usr/bin/systemctl reboot"


class RestartJournalMachine:
    """A machine whose PID 1 journal reports the given unit failures."""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal
        self.unit_logs_requested: list[str] = []

    async def ssh_command(self, *args: str, **_kwargs: object) -> SimpleNamespace:
        if "-u" in args:
            self.unit_logs_requested.append(args[args.index("-u") + 1])
        return SimpleNamespace(stdout=self.journal, returncode=0)


def test_podman_healthcheck_units_are_not_treated_as_failures() -> None:
    """podman's startup probes exit non-zero by design until a container is healthy.

    Every container contributes one, so counting them would make the settled
    boot permanently red and bury the real failures among them.
    """
    container = "a" * 64
    journal = [
        f"lab systemd[1]: {container}-startup-784185405a4177c2.service: Main process exited, code=exited, status=1/FAILURE",
        "lab systemd[1]: jellyfin.service: Main process exited, code=exited, status=125/n/a",
        "lab systemd[1]: jellyfin.service: Scheduled restart job, restart counter is at 1.",
    ]
    machine = RestartJournalMachine(journal)

    restarted = asyncio.run(site_test.report_restarted_units(cast(site_test.Machine, machine), journal))

    assert restarted == ["jellyfin.service"]
    assert machine.unit_logs_requested == ["jellyfin.service"]


def test_a_unit_that_recovers_still_fails_the_converge() -> None:
    """The whole point: recovery on a retry hides the failure from is-system-running."""
    journal = ["lab systemd[1]: headscale.service: Failed with result 'exit-code'."]
    machine = RestartJournalMachine(journal)

    restarted = asyncio.run(site_test.report_restarted_units(cast(site_test.Machine, machine), journal))

    assert restarted == ["headscale.service"]


def test_failed_non_service_units_fail_the_converge() -> None:
    journal = ["lab systemd[1]: mnt-media.mount: Failed with result 'exit-code'."]
    machine = RestartJournalMachine(journal)

    restarted = asyncio.run(site_test.report_restarted_units(cast(site_test.Machine, machine), journal))

    assert restarted == ["mnt-media.mount"]


def test_a_recovered_unit_fails_the_converge_after_the_guest_powered_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = SiteTestMachine(
        tmp_path,
        pid1_journal=["lab systemd[1]: jellyfin.service: Failed with result 'exit-code'."],
    )

    with pytest.raises(site_test.UnitRestartedError, match=r"jellyfin\.service"):
        asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10))

    # The shutdown path is still exercised, so a wedged stop job is not hidden.
    assert ("sudo", "systemctl", "--check-inhibitors=no", "poweroff") in machine.ssh_calls


def test_a_fleet_that_never_settles_fails_before_the_poweroff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("machine.cancel_on_signal", lambda _task: contextlib.nullcontext())
    machine = SiteTestMachine(tmp_path, settle_exitcode=124)

    with pytest.raises(site_test.SettleTimeoutError):
        asyncio.run(site_test.run_site_test(cast(site_test.Machine, machine), timeout=10))

    assert ("sudo", "systemctl", "--check-inhibitors=no", "poweroff") not in machine.ssh_calls


@pytest.mark.parametrize(
    "suffix",
    [
        "-startup.service",
        ".service",
        ".timer",
        "-startup-784185405a4177c2.service",
        "-784185405a4177c2.service",
        "-784185405a4177c2.timer",
    ],
)
def test_podman_healthcheck_transient_units_are_not_failures(suffix: str) -> None:
    assert site_test.PODMAN_HEALTHCHECK_UNIT.match("a" * 64 + suffix)


def test_lookalike_units_are_still_failures() -> None:
    assert not site_test.PODMAN_HEALTHCHECK_UNIT.match("a" * 63 + ".service")
    assert not site_test.PODMAN_HEALTHCHECK_UNIT.match("a" * 64 + "-startup-not-hex.service")
    assert not site_test.PODMAN_HEALTHCHECK_UNIT.match("a" * 64 + ".mount")
