"""Exact-output tests for Machine.format_{ssh,ansible}_cmd."""

import asyncio
import shlex
from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def test_harness_and_ansible_share_one_agent_forwarding_master(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    """Whichever side connects first opens the cell's master, and it must
    carry the forwarded agent that roles cloning from GitHub use."""
    m = machine_factory(ssh_port=2222, ssh_user="vagrant")
    ssh = m.format_ssh_cmd()
    ansible_ssh_args = m.ansible_env()["ANSIBLE_SSH_ARGS"].split()

    for option in (f"ControlPath={m.ssh_control_path}", "ControlMaster=auto", "ForwardAgent=yes"):
        assert option in ssh
        assert option in ansible_ssh_args
    assert ssh[-1] == "vagrant@127.0.0.1"


def test_format_ssh_cmd_quotes_the_remote_command(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory()
    cmd = m.format_ssh_cmd("echo", "hello world")

    # ssh joins trailing args into one remote command, so the harness passes a
    # single shell-quoted positional after user@host.
    assert cmd[-2] == f"{m.ssh_user}@{machine.SSH_HOST}"
    assert cmd[-1] == "echo 'hello world'"


def test_format_ansible_cmd_default_envelope(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(
        ssh_port=2222,
        ssh_user="vagrant",
    )
    cmd = m.format_ansible_cmd("site.yml")

    assert cmd[0] == "ansible-playbook"

    # Connection vars ride a per-cell inventory, as in hosts.ini, never -e:
    # extra vars would mask what delegated tasks see in production.
    assert not any("ansible_ssh_" in part and "common_args" not in part for part in cmd)
    inventories = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--inventory"]
    assert inventories == ["test/inventory.ini", str(m.connection_inventory_path)]
    m._write_connection_inventory()
    assert m.connection_inventory_path.read_text() == (
        f"{m.machine} ansible_ssh_host=127.0.0.1 ansible_ssh_port=2222"
        " ansible_ssh_user=vagrant ansible_ssh_private_key_file=packer/vagrant.key\n"
    )

    # --limit pins the static `hosts: all` playbook to the inventory host
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == m.machine
    # The role name reaches the static dispatcher as an extra var.
    assert f"_test_role_under_test={m.role}" in cmd

    # Trailing positional
    assert cmd[-1] == "site.yml"

    # Default: the harness selects Nexus through its test-only input.
    assert "nexus_url=" not in cmd

    # Harness inputs are indirect so Ansible task vars can override the public
    # environment variables during fixture coverage.
    assert '{"_test_in_aws":false,"_test_nexus_url":"nexus.lab.fahm.fr"}' in cmd
    assert not any("tailscale_wan_direct" in part for part in cmd)


@pytest.mark.parametrize(
    ("playbook_name", "phase_args", "expected_arg"),
    [
        ("site.yml", ("-e", "_role_tasks_from=_verify", "--tags", "homepage"), "_role_tasks_from=_verify"),
        ("_environment.yml", ("-e", "test_base_prerequisites=false"), "test_base_prerequisites=false"),
    ],
)
def test_kept_vm_resume_command_targets_fixture(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
    playbook_name: str,
    phase_args: tuple[str, ...],
    expected_arg: str,
) -> None:
    m = machine_factory(keep_vm=True, ssh_port=2222)
    m.vnc_port = 5900
    lines: list[str] = []
    monkeypatch.setattr(machine, "print_line", lines.append)
    monkeypatch.setattr(m, "_stage_ansible_controller", lambda: None)

    async def fake_run_command(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(machine, "run_command", fake_run_command)
    playbook = str(m.workdir_path / playbook_name)
    asyncio.run(m.ansible_command(playbook, *phase_args))

    m.print_ssh_instructions()

    resume = next(line for line in lines if line.startswith("> env "))
    parts = shlex.split(resume.removeprefix("> "))
    assert "ansible-playbook" in parts
    assert parts[parts.index("--inventory") + 1] == "test/inventory.ini"
    assert parts[parts.index("--limit") + 1] == "lab"
    assert "--start-at-task" not in parts
    assert playbook in parts
    assert expected_arg in parts
    if "--tags" in phase_args:
        assert parts[parts.index("--tags") + 1] == "homepage"
    assert str(m.connection_inventory_path) in parts
    assert any("uses staged code" in line for line in lines)
    assert any("--step" in line for line in lines)


def test_kept_vm_without_ansible_only_prints_ssh(
    machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machine_factory(keep_vm=True)
    m.vnc_port = 5900
    lines: list[str] = []
    monkeypatch.setattr(machine, "print_line", lines.append)

    m.print_ssh_instructions()

    assert any(line.startswith("> ssh ") for line in lines)
    assert not any("ansible-playbook" in line for line in lines)


def test_format_ansible_cmd_in_aws_env_sets_flag_and_clears_nexus(
    machine_factory: Callable[..., machine.Machine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The aws_qemu cell runs the qemu backend on an AWS host: HOMELAB_TEST_IN_AWS
    # flips test_in_aws true (so roles pick the upstream mirrors / public DNS)
    # and clears nexus_url even without --upstream-mirrors, because the LAN
    # Nexus is unreachable from AWS.
    monkeypatch.setenv("HOMELAB_TEST_IN_AWS", "true")
    m = machine_factory()
    cmd = m.format_ansible_cmd("site.yml")

    assert '{"_test_in_aws":true,"_test_nexus_url":""}' in cmd
    assert "nexus_url=" not in cmd


def test_ecr_login_uses_shared_aws_region() -> None:
    login_lines = [
        line
        for line in Path("test/playbooks/_environment.yml").read_text().splitlines()
        if "ecr get-login-password" in line
    ]

    assert len(login_lines) == 1
    assert "test_aws_region" in login_lines[0]


def test_ansible_env_default_envelope(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory()
    env = m.ansible_env()
    assert env["ANSIBLE_DISPLAY_OK_HOSTS"] == "true"
    assert env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] == "true"
    assert env["ANSIBLE_GATHERING"] == "smart"
    assert env["ANSIBLE_TIMEOUT"] == "30"
    assert env["ANSIBLE_FACT_CACHING"] == "jsonfile"
    assert env["ANSIBLE_FACT_CACHING_CONNECTION"] == str(m.workdir_path / "facts")
    assert env["ANSIBLE_FACT_CACHING_TIMEOUT"] == "7200"


def test_ansible_env_quiet_runs_suppress_verbose_output(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    env = machine_factory(quiet_ansible=True).ansible_env()

    assert env["ANSIBLE_DISPLAY_OK_HOSTS"] == "false"
    assert env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] == "false"
    assert env["ANSIBLE_VERBOSITY"] == "0"
    assert env["PROFILE_TASKS_TASK_OUTPUT_LIMIT"] == "all"


def test_journal_console_is_always_attached(machine_factory: Callable[..., machine.Machine]) -> None:
    """Every cell carries the virtio console the image mirrors its journal onto.

    The image's journald drop-in points at /dev/hvc0 unconditionally, so a
    missing device would cost a failed open per forwarded message and leave
    the run with no journal at all.
    """

    m = machine_factory()
    # prepare() builds these; the command assembly only reads them.
    m.drives = ["file=disk.qcow2,if=virtio"]
    cmd = " ".join(m._boot_command())

    assert "virtio-serial-pci,id=journal_bus" in cmd
    assert f"file,id=journal,path={m.journal_file}" in cmd
    assert "virtconsole,chardev=journal,bus=journal_bus.0" in cmd


def test_format_ansible_cmd_upstream_mirrors_clears_nexus(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(upstream_mirrors=True)
    cmd = m.format_ansible_cmd("site.yml")

    assert any('"_test_nexus_url":""' in part for part in cmd)
    assert "nexus_url=" not in cmd
