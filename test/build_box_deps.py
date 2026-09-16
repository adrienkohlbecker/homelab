#!/usr/bin/env -S uv run
"""Build the mutable box_deps fixture without exposing image writes through launch.py."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from machine import SSH_HOST, LaunchOptions, Machine
from matrix import DEFAULT_UBUNTU, UBUNTU_RELEASES
from utils import print_line, tee_output

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_TIMEOUT = 1200
HYDRATED_MANIFEST_NAME = ".homelab_s3_manifest.json"


@dataclass(frozen=True)
class BaseProvenance:
    build_id: str
    source_sha: str
    architecture: str


def base_provenance_from_env() -> BaseProvenance | None:
    """Return the explicitly selected hydrated base, requiring all fields."""

    values = {
        "build_id": os.environ.get("HOMELAB_BOX_BASE_BUILD_ID", "").strip(),
        "source_sha": os.environ.get("HOMELAB_BOX_BASE_SOURCE_SHA", "").strip(),
        "architecture": os.environ.get("HOMELAB_BOX_BASE_ARCHITECTURE", "").strip(),
    }
    if not any(values.values()):
        return None
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise RuntimeError(f"Incomplete box base provenance; missing: {', '.join(missing)}")
    if len(values["source_sha"]) not in (40, 64) or any(
        character not in "0123456789abcdef" for character in values["source_sha"]
    ):
        raise RuntimeError("HOMELAB_BOX_BASE_SOURCE_SHA must be a full lowercase Git object id")
    if values["architecture"] not in ("x86_64", "aarch64"):
        raise RuntimeError(f"Unsupported box base architecture: {values['architecture']}")
    return BaseProvenance(**values)


def validate_base_provenance(source: Path, ubuntu: str, expected: BaseProvenance) -> None:
    """Fail unless a hydrated box tree matches the explicitly selected base."""

    manifest_path = source / HYDRATED_MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"Hydrated box manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hydrated box manifest is invalid JSON: {manifest_path}") from exc
    expected_fields = {
        "machine": "box",
        "ubuntu": ubuntu,
        "build_id": expected.build_id,
        "source_sha": expected.source_sha,
        "architecture": expected.architecture,
    }
    mismatches = {
        name: {"expected": value, "actual": manifest.get(name)}
        for name, value in expected_fields.items()
        if manifest.get(name) != value
    }
    if mismatches:
        raise RuntimeError(f"Hydrated box provenance mismatch: {json.dumps(mismatches, sort_keys=True)}")


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
        launch=LaunchOptions(image_dir=image_dir, headless=True, write_image=True),
        loopback_host=SSH_HOST,
    )
    with tee_output(machine.output_file):
        async with machine.session(BUILD_TIMEOUT):
            await machine.ensure_booted()
            print_line("Booted")
            await machine.ensure_ssh()
            print_line("SSH up")

            await machine.ensure_system_running()

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


def build_one(root: Path, ubuntu: str, base_provenance: BaseProvenance | None = None) -> None:
    """Build and atomically publish one Ubuntu box_deps fixture."""

    source = root / ubuntu / "box"
    destination = root / ubuntu / "box_deps"
    if not source.is_dir():
        raise RuntimeError(
            f"Source box artifacts missing at {source}\nRun 'mise run packer:build box --ubuntu {ubuntu}' first."
        )
    if base_provenance is not None:
        validate_base_provenance(source, ubuntu, base_provenance)

    staging = Path(tempfile.mkdtemp(prefix=f".build-box-deps-{ubuntu}-", dir=root))
    staging.chmod(0o2770)
    try:
        print_line(f"==> Staging {source} -> {staging}")
        clone_artifacts(source, staging)
        staging.chmod(0o2770)
        asyncio.run(seed_image(staging, ubuntu))
        if base_provenance is not None:
            validate_base_provenance(staging, ubuntu, base_provenance)
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
    base_provenance = base_provenance_from_env()
    for ubuntu in ubuntus:
        build_one(root, ubuntu, base_provenance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
