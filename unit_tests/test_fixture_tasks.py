"""Behavioral tests for the derived QEMU fixture builder."""

from __future__ import annotations

import asyncio
import contextlib
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

    builder.build_one(root, "noble")

    destination = root / "noble" / "box_deps"
    assert (destination / "artifact").read_text() == "source\n"
    assert (destination / "seeded").read_text() == "yes\n"
    assert destination.stat().st_mode & 0o7777 == 0o2770


def test_main_builds_each_requested_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str]] = []
    monkeypatch.setenv("HOMELAB_CI_DIR", str(tmp_path))
    monkeypatch.setenv("usage_ubuntu", "noble resolute")
    monkeypatch.setattr(builder, "build_one", lambda root, ubuntu: calls.append((root, ubuntu)))

    assert builder.main() == 0
    assert calls == [(tmp_path, "noble"), (tmp_path, "resolute")]


def test_seed_image_uses_private_writeback_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    constructor: dict[str, object] = {}

    class FakeMachine:
        output_file = tmp_path / "output"
        workdir_path = tmp_path

        async def __aenter__(self) -> FakeMachine:
            calls.append("enter")
            return self

        async def __aexit__(self, *args: object) -> None:
            calls.append("exit")

        async def ensure_booted(self) -> None:
            calls.append("booted")

        async def ensure_ssh(self) -> None:
            calls.append("ssh")

        async def ssh_command(self, *args: str, check: bool = True) -> SimpleNamespace:
            calls.append((args, check))
            if args[:2] == ("systemctl", "is-system-running"):
                return SimpleNamespace(exitcode=0, stdout=["running"])
            return SimpleNamespace(exitcode=0, stdout=[])

        async def ansible_command(self, playbook: str) -> None:
            calls.append(("ansible", playbook))

        async def wait(self) -> None:
            calls.append("wait")

    def machine_factory(**kwargs: object) -> FakeMachine:
        constructor.update(kwargs)
        return FakeMachine()

    monkeypatch.setattr(builder, "Machine", machine_factory)
    monkeypatch.setattr(builder, "cancel_on_signal", lambda task: contextlib.nullcontext())

    asyncio.run(builder.seed_image(tmp_path, "noble"))

    assert constructor["write_image"] is True
    assert constructor["loopback_host"] == builder.SSH_HOST
    launch = cast(LaunchOptions, constructor["launch"])
    assert launch.image_dir == tmp_path
    assert launch.headless is True
    assert ("ansible", str(tmp_path / "build_box_deps.yml")) in calls
    assert "wait" in calls
