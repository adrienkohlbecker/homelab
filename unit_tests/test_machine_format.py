"""Exact-output tests for Machine.format_{ssh,ansible}_cmd."""

import asyncio
import shlex
from collections.abc import Callable
from pathlib import Path

import machine
import pytest


def test_format_ssh_cmd_no_remote_returns_bare_prefix(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(ssh_port=2222, ssh_user="vagrant")
    assert m.format_ssh_cmd() == [
        "ssh",
        "-i",
        "packer/vagrant.key",
        "-p",
        "2222",
        "-o",
        "ControlPath=/tmp/homelab-cm-127.0.0.1-2222",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=600s",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "BatchMode=yes",
        "-o",
        "ForwardAgent=yes",
        "vagrant@127.0.0.1",
    ]


def test_format_ssh_cmd_with_remote_appends_shlex_joined_arg(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory()
    cmd = m.format_ssh_cmd("ls", "-la", "/etc/hostname")
    # The remote command is collapsed into a single positional after user@host
    # (ssh treats trailing args as the remote command, but shlex.join keeps
    # quoting intact when one of the args contains a space).
    assert cmd[-2] == f"{m.ssh_user}@{machine.SSH_HOST}"
    assert cmd[-1] == shlex.join(("ls", "-la", "/etc/hostname"))


def test_format_ssh_cmd_quotes_remote_with_spaces(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory()
    cmd = m.format_ssh_cmd("echo", "hello world")
    # shlex.join quotes the second arg because of the space.
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

    # ansible_ssh_* overrides
    assert "ansible_ssh_port=2222" in cmd
    assert "ansible_ssh_host=127.0.0.1" in cmd
    assert "ansible_ssh_user=vagrant" in cmd
    assert "ansible_ssh_private_key_file=packer/vagrant.key" in cmd

    # The test inventory supplies group vars at inventory precedence.
    assert "--inventory" in cmd
    assert cmd[cmd.index("--inventory") + 1] == "test/inventory.ini"

    # --limit pins the static `hosts: all` playbook to the inventory host
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == m.inventory_host
    # The internal input injects the immutable role name into the static
    # dispatcher; group_vars publishes _role_under_test at normal precedence.
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
    m.vnc_display = 0
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
    assert "ansible_ssh_port=2222" in parts
    assert any("uses staged code" in line for line in lines)
    assert any("--step" in line for line in lines)


def test_kept_vm_without_ansible_only_prints_ssh(
    machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machine_factory(keep_vm=True)
    m.vnc_display = 0
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
    # flips test_in_aws true (so roles pick the regional mirrors / public DNS)
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
    env = machine_factory(run_options=machine.MachineRunOptions(quiet_ansible=True)).ansible_env()

    assert env["ANSIBLE_DISPLAY_OK_HOSTS"] == "false"
    assert env["ANSIBLE_DISPLAY_SKIPPED_HOSTS"] == "false"
    assert env["ANSIBLE_VERBOSITY"] == "0"


def test_run_options_override_machine_resources(machine_factory: Callable[..., machine.Machine]) -> None:
    m = machine_factory(run_options=machine.MachineRunOptions(vcpus=6, memory_mb=12288))

    assert m._spec.vcpus == 6
    assert m._spec.memory_mb == 12288


def test_format_ansible_cmd_upstream_mirrors_clears_nexus(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(upstream_mirrors=True)
    cmd = m.format_ansible_cmd("site.yml")

    assert any('"_test_nexus_url":""' in part for part in cmd)
    assert "nexus_url=" not in cmd


def test_format_ansible_cmd_no_positional(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory()
    cmd = m.format_ansible_cmd()

    assert cmd[0] == "ansible-playbook"
    assert "ansible-playbook" in cmd
    assert not any(part.endswith((".yml", ".yaml")) for part in cmd)
