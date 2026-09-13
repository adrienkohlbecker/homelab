"""Behavioral tests for the derived QEMU fixture builder."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import build_box_deps as builder
import pytest
from machine import LaunchOptions


def test_clone_artifacts_copies_the_fixture_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "artifact").write_text("fixture\n")

    builder.clone_artifacts(source, destination)

    assert (destination / "artifact").read_text() == "fixture\n"


def test_build_one_clones_seeds_and_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "homelab_ci"
    source = root / "noble" / "box"
    source.mkdir(parents=True)
    (source / "artifact").write_text("source\n")
    provenance = builder.BaseProvenance(build_id="base-123", source_sha="d" * 40, architecture="aarch64")
    (source / builder.HYDRATED_BUILD_ID_NAME).write_text(f"{provenance.build_id}\n")
    (source / builder.HYDRATED_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "architecture": provenance.architecture,
                "build_id": provenance.build_id,
                "machine": "box",
                "source_sha": provenance.source_sha,
                "ubuntu": "noble",
            }
        )
    )

    def clone_artifacts(src: Path, dst: Path) -> None:
        shutil.copytree(src, dst, dirs_exist_ok=True)

    async def seed_image(image_dir: Path, ubuntu: str) -> None:
        assert ubuntu == "noble"
        (image_dir / "seeded").write_text("yes\n")

    def publish_artifacts(publish_root: Path, src: Path, dst: Path) -> None:
        assert publish_root == root
        os.replace(src, dst)

    monkeypatch.setattr(builder, "clone_artifacts", clone_artifacts)
    monkeypatch.setattr(builder, "seed_image", seed_image)
    monkeypatch.setattr(builder, "publish_artifacts", publish_artifacts)

    builder.build_one(root, "noble", provenance)

    destination = root / "noble" / "box_deps"
    assert (destination / "artifact").read_text() == "source\n"
    assert (destination / "seeded").read_text() == "yes\n"
    assert (destination / builder.HYDRATED_BUILD_ID_NAME).read_text().strip() == provenance.build_id
    assert destination.stat().st_mode & 0o7777 == 0o2770


def test_build_one_rejects_wrong_hydrated_base_before_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "homelab_ci"
    source = root / "noble" / "box"
    source.mkdir(parents=True)
    (source / builder.HYDRATED_BUILD_ID_NAME).write_text("older-build\n")
    (source / builder.HYDRATED_MANIFEST_NAME).write_text("{}\n")
    provenance = builder.BaseProvenance(build_id="requested-build", source_sha="d" * 40, architecture="aarch64")
    monkeypatch.setattr(
        builder,
        "clone_artifacts",
        lambda *_: pytest.fail("mismatched provenance must be rejected before cloning"),
    )

    with pytest.raises(RuntimeError, match="does not match requested base"):
        builder.build_one(root, "noble", provenance)


def test_main_builds_each_requested_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str]] = []
    monkeypatch.setenv("HOMELAB_CI_DIR", str(tmp_path))
    monkeypatch.setenv("usage_ubuntu", "noble resolute")
    monkeypatch.setattr(builder, "build_one", lambda root, ubuntu, provenance: calls.append((root, ubuntu)))

    assert builder.main() == 0
    assert calls == [(tmp_path, "noble"), (tmp_path, "resolute")]


def test_base_provenance_requires_complete_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_BOX_BASE_BUILD_ID", "base-123")

    with pytest.raises(RuntimeError, match="missing: architecture, source_sha"):
        builder.base_provenance_from_env()


def test_box_deps_playbook_keeps_aws_mirrors_and_rejects_lab_nexus() -> None:
    playbook = (builder.REPO_ROOT / "test" / "playbooks" / "build_box_deps.yml").read_text()

    assert "Configure apt for the build environment" in playbook
    assert playbook.count("when: not test_in_aws") == 3
    assert "when: test_in_aws" in playbook
    assert "ubuntu_mirror | urlsplit('hostname')" in playbook
    assert r"contains: nexus\.lab\.fahm\.fr" in playbook


def test_seed_image_uses_private_writeback_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    constructor: dict[str, object] = {}

    class FakeMachine:
        output_file = tmp_path / "output"
        workdir_path = tmp_path

        @contextlib.asynccontextmanager
        async def session(self, timeout: int):
            calls.append(("session", timeout))
            async with self:
                yield

        async def __aenter__(self) -> FakeMachine:
            calls.append("enter")
            return self

        async def __aexit__(self, *args: object) -> None:
            calls.append("exit")

        async def ensure_booted(self) -> None:
            calls.append("booted")

        async def ensure_ssh(self) -> None:
            calls.append("ssh")

        async def ensure_system_running(self) -> None:
            calls.append("system_running")

        async def ssh_command(self, *args: str, check: bool = True) -> SimpleNamespace:
            calls.append((args, check))
            return SimpleNamespace(exitcode=0, stdout=[])

        async def ansible_command(self, playbook: str) -> None:
            calls.append(("ansible", playbook))

        async def wait(self) -> None:
            calls.append("wait")

    def machine_factory(**kwargs: object) -> FakeMachine:
        constructor.update(kwargs)
        return FakeMachine()

    monkeypatch.setattr(builder, "Machine", machine_factory)

    asyncio.run(builder.seed_image(tmp_path, "noble"))

    assert constructor["loopback_host"] == builder.SSH_HOST
    launch = cast(LaunchOptions, constructor["launch"])
    assert launch.image_dir == tmp_path
    assert launch.headless is True
    assert launch.write_image is True
    assert ("session", builder.BUILD_TIMEOUT) in calls
    assert "system_running" in calls
    assert ("ansible", str(tmp_path / "build_box_deps.yml")) in calls
    assert "wait" in calls
