"""Tests for the small Machine / QemuMachine / PodmanMachine properties.

Covers wrapper_timeout (Machine), the cached ArchProfile on QemuMachine,
and image_tag (PodmanMachine).
"""

from collections.abc import Callable
from pathlib import Path

import pytest

import machine


def test_wrapper_timeout_is_machine_timeout_when_not_keeping(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(machine_timeout=600, keep_vm=False)
    assert m.wrapper_timeout == 600


def test_wrapper_timeout_is_zero_when_keeping(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    # `timeout 0` and `podman --timeout 0` both mean "no limit"; this is what
    # lets --keep sessions stay up indefinitely.
    m = machine_factory(machine_timeout=600, keep_vm=True)
    assert m.wrapper_timeout == 0


@pytest.mark.parametrize(
    "platform_machine,expected",
    [
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("aarch64", "aarch64"),
        ("arm64", "aarch64"),
    ],
)
def test_arch_profile_normalises_known_machines(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
    platform_machine: str,
    expected: str,
) -> None:
    m = qemu_machine_factory(host_arch=platform_machine)
    assert m.arch.name == expected


def test_unknown_host_arch_fails_fast_at_construction(
    qemu_machine_factory: Callable[..., machine.QemuMachine],
) -> None:
    # detect_host_arch() runs once inside QemuMachine.__init__, so an
    # unsupported platform raises before the instance exists.
    with pytest.raises(RuntimeError, match="Unsupported host architecture"):
        qemu_machine_factory(host_arch="riscv64")


def test_image_tag_for_non_service_role(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
) -> None:
    # is_service_role looks for roles/<role>/tasks/_test.yml; with no such
    # file present (the chdir'd tmp_path is empty), the role is non-service.
    m = podman_machine_factory(role="vanilla", ubuntu_name="jammy")
    assert m.image_tag == "homelab:jammy"


def test_image_tag_for_service_role(
    podman_machine_factory: Callable[..., machine.PodmanMachine],
    tmp_path: Path,
) -> None:
    # Stage a role with a podman _test import so is_service_role flips.
    role_dir = tmp_path / "roles" / "myservice" / "tasks"
    role_dir.mkdir(parents=True)
    (role_dir / "_test.yml").write_text("- import_role:\n    tasks_from: podman\n")

    m = podman_machine_factory(role="myservice", ubuntu_name="noble")
    assert m.image_tag == "homelab-service:noble"
