#!/usr/bin/env -S uv run
"""Build the mutable box_deps fixture without exposing image writes through launch.py."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from machine import SSH_HOST, LaunchOptions, Machine
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import cancel_on_signal, print_line, tee_output

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_TIMEOUT = 1200


def clone_artifacts(source: Path, destination: Path) -> None:
    """Clone a published image tree while preserving copy-on-write support."""

    system = platform.system()
    if system == "Linux":
        command = ["cp", "-R", "--reflink=auto", f"{source}/.", str(destination)]
    elif system == "Darwin":
        command = ["ditto", str(source), str(destination)]
    else:
        raise RuntimeError(f"Unsupported OS: {system}")
    subprocess.run(command, check=True)


async def seed_image(image_dir: Path, ubuntu: str) -> None:
    """Boot a staged image, converge its dependencies, and power it off."""

    machine = Machine(
        machine="box_deps",
        role="_box_deps_build",
        keep_vm=False,
        ubuntu_name=ubuntu,
        machine_timeout=BUILD_TIMEOUT,
        launch=LaunchOptions(image_dir=image_dir, headless=True),
        loopback_host=SSH_HOST,
        write_image=True,
    )
    task = asyncio.current_task()
    assert task is not None
    with tee_output(machine.output_file), cancel_on_signal(task):
        async with machine:
            await machine.ensure_booted()
            print_line("Booted")
            await machine.ensure_ssh()
            print_line("SSH up")

            result = await machine.ssh_command("systemctl", "is-system-running", "--wait", check=False)
            state = "\n".join(result.stdout).strip()
            if result.exitcode != 0 or state != "running":
                failed = await machine.ssh_command("systemctl", "--failed", "--no-legend", check=False)
                failed_units = "\n".join(failed.stdout).rstrip() or "(none)"
                raise RuntimeError(f"System reached state {state!r}; failed units:\n{failed_units}")
            print_line(f"System fully booted: {state}")

            playbook = machine.workdir_path / "build_box_deps.yml"
            print_line("Seeding image via test/playbooks/build_box_deps.yml")
            await machine.ansible_command(str(playbook))
            print_line("Seed playbook complete; powering off")
            await machine.ssh_command("sudo", "systemctl", "poweroff", check=False)
            await machine.wait()


def publish_artifacts(root: Path, source: Path, destination: Path) -> None:
    """Publish through the lock shared with Packer and running test cells."""

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "packer" / "publish.py"),
            str(root / ".publish-lock"),
            str(source),
            str(destination),
        ],
        check=True,
    )


def build_one(root: Path, ubuntu: str) -> None:
    """Build and atomically publish one Ubuntu box_deps fixture."""

    source = root / ubuntu / "box"
    destination = root / ubuntu / "box_deps"
    if not source.is_dir():
        raise RuntimeError(
            f"Source box artifacts missing at {source}\nRun 'mise run packer:build box --ubuntu {ubuntu}' first."
        )

    staging = Path(tempfile.mkdtemp(prefix=f".build-box-deps-{ubuntu}-", dir=root))
    staging.chmod(0o2770)
    try:
        print_line(f"==> Staging {source} -> {staging}")
        clone_artifacts(source, staging)
        staging.chmod(0o2770)
        asyncio.run(seed_image(staging, ubuntu))
        print_line(f"==> Publishing {staging} -> {destination}")
        publish_artifacts(root, staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print_line(f"==> box_deps published at {destination}")


def main() -> int:
    os.umask(0o002)
    root_value = os.environ.get("HOMELAB_CI_DIR")
    if not root_value:
        raise RuntimeError("HOMELAB_CI_DIR is required")
    root = Path(root_value)
    root.mkdir(parents=True, exist_ok=True)

    ubuntus = os.environ.get("usage_ubuntu", DEFAULT_UBUNTU).split()
    unknown = sorted(set(ubuntus) - set(UBUNTU_RELEASES))
    if unknown:
        raise RuntimeError(f"Unknown Ubuntu release(s): {', '.join(unknown)}")
    for ubuntu in ubuntus:
        build_one(root, ubuntu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
