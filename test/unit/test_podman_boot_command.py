"""Exact-output tests for PodmanMachine._boot_command."""

from collections.abc import Callable

import machine


def test_default_invocation(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
) -> None:
    m = podman_machine_factory(
        machine="container",
        role="rolex",
        ubuntu_name="jammy",
        keep_vm=False,
        machine_timeout=300,
    )
    assert m._boot_command() == [
        "podman",
        "run",
        "--rm",
        "--timeout",
        "300",
        "--systemd",
        "always",
        "--hostname",
        "box",
        "--publish",
        "127.0.0.1::22",
        "--privileged",
        "--cidfile",
        f"{m.workdir.name}/cid",
        "--network",
        "homelab_net",
        "homelab:jammy",
    ]


def test_keep_vm_disables_timeout(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
) -> None:
    m = podman_machine_factory(keep_vm=True, machine_timeout=300)
    cmd = m._boot_command()
    # podman --timeout 0 means "no timeout" -- mirrors what keep_vm does to
    # the GNU timeout wrapper on the qemu side.
    assert cmd[cmd.index("--timeout") + 1] == "0"


def test_uses_service_image_tag_for_service_role(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
    tmp_path,
) -> None:
    # Stage a podman-importing _setup.yml so image_tag flips to homelab-service.
    role_dir = tmp_path / "roles" / "edge" / "tasks"
    role_dir.mkdir(parents=True)
    (role_dir / "_setup.yml").write_text("- import_role:\n    tasks_from: nginx\n")

    m = podman_machine_factory(role="edge", ubuntu_name="noble")
    # Image tag is the very last positional in the podman cmdline.
    assert m._boot_command()[-1] == "homelab-service:noble"
