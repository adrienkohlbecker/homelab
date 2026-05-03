"""Exact-output tests for Machine.format_{ssh,scp,ansible}_cmd."""

import shlex
from collections.abc import Callable

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
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
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


def test_format_scp_cmd_uses_capital_P_and_drops_forward_agent(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(ssh_port=2222, ssh_user="vagrant")
    assert m.format_scp_cmd("local.sh", "/tmp/remote.sh") == [
        "scp",
        "-i",
        "packer/vagrant.key",
        "-P",
        "2222",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "BatchMode=yes",
        "local.sh",
        "vagrant@127.0.0.1:/tmp/remote.sh",
    ]


def test_format_ansible_cmd_default_envelope(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(
        ssh_port=2222,
        ssh_user="vagrant",
        ansible_args=["-e", '{"qemu_test":true}', "-e", "@host_vars/box-qemu.yml"],
    )
    cmd = m.format_ansible_cmd("site.yml")

    assert cmd[0] == "env"
    # Ansible env vars precede the binary
    assert "ANSIBLE_DISPLAY_OK_HOSTS=true" in cmd
    assert "ANSIBLE_DISPLAY_SKIPPED_HOSTS=true" in cmd
    assert "ANSIBLE_GATHERING=smart" in cmd
    assert "ANSIBLE_FACT_CACHING=jsonfile" in cmd
    assert f"ANSIBLE_FACT_CACHING_CONNECTION={m.workdir.name}/facts" in cmd
    assert "ANSIBLE_FACT_CACHING_TIMEOUT=7200" in cmd

    binary_idx = cmd.index("ansible-playbook")
    # All env-style entries must come before the binary
    assert all("=" in c for c in cmd[1:binary_idx])

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
        ansible_args=["-e", '{"qemu_test":true}', "-e", "@host_vars/box-qemu.yml"],
    )
    cmd = m.format_ansible_cmd()

    assert cmd[0] == "env"
    assert "ansible-playbook" in cmd
    # With no extra cmd args the tail is whatever the spec's ansible_args ended in
    assert cmd[-1] == m.ansible_args[-1]
