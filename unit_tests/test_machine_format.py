"""Exact-output tests for Machine.format_{ssh,ansible}_cmd."""

import shlex
from collections.abc import Callable

import pytest

import machine


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
        "ControlPath=/tmp/homelab-cm-2222",
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
        ansible_args=["-e", "@host_vars/lab-qemu.yml"],
    )
    cmd = m.format_ansible_cmd("site.yml")

    assert cmd[0] == "ansible-playbook"

    # ansible_ssh_* overrides
    assert "ansible_ssh_port=2222" in cmd
    assert "ansible_ssh_host=127.0.0.1" in cmd
    assert "ansible_ssh_user=vagrant" in cmd
    assert "ansible_ssh_private_key_file=packer/vagrant.key" in cmd

    # Inventory + spec ansible_args present
    assert "--inventory" in cmd
    assert cmd[cmd.index("--inventory") + 1] == "test/inventory.ini"
    for arg in m.ansible_args:
        assert arg in cmd

    # --limit pins the static `hosts: all` playbook to the inventory host
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == m.inventory_host
    # _role_under_test injects the role name into the static playbooks'
    # `import_role: name: "{{ _role_under_test }}"` references.
    assert f"_role_under_test={m.role}" in cmd

    # Trailing positional
    assert cmd[-1] == "site.yml"

    # Default: no upstream-mirrors override
    assert "nexus_url=" not in cmd

    # Cloud-environment discriminator. With HOMELAB_TEST_IN_AWS unset, the
    # guest is not in AWS.
    assert '{"test_in_aws": false}' in cmd
    assert not any("tailscale_wan_direct" in part for part in cmd)
    assert not any("headscale_oidc_enabled" in part for part in cmd)
    assert not any("podman_zvol_size" in part for part in cmd)


def test_format_ansible_cmd_role_fixture_vars(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    firewall_cmd = machine_factory(role="firewall").format_ansible_cmd("site.yml")
    headscale_cmd = machine_factory(role="headscale").format_ansible_cmd("site.yml")
    site_cmd = machine_factory(role="_site_test").format_ansible_cmd("site.yml")

    assert '{"tailscale_wan_direct":true}' in firewall_cmd
    assert '{"headscale_oidc_enabled":true}' in headscale_cmd
    assert '{"podman_zvol_size":"53687091200"}' in site_cmd


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

    assert '{"test_in_aws": true}' in cmd
    assert "nexus_url=" in cmd
    assert cmd.index("nexus_url=") < cmd.index("site.yml")


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


def test_format_ansible_cmd_upstream_mirrors_clears_nexus(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(upstream_mirrors=True)
    cmd = m.format_ansible_cmd("site.yml")

    assert "nexus_url=" in cmd
    # The override must come before the trailing positional so ansible
    # parses it as a -e var, not as a playbook path.
    assert cmd.index("nexus_url=") < cmd.index("site.yml")


def test_format_ansible_cmd_no_positional(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(
        ansible_args=["-e", "@host_vars/lab-qemu.yml"],
    )
    cmd = m.format_ansible_cmd()

    assert cmd[0] == "ansible-playbook"
    assert "ansible-playbook" in cmd
    # With no extra cmd args the tail is whatever the spec's ansible_args ended in
    assert cmd[-1] == m.ansible_args[-1]
