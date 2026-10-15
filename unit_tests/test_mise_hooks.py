"""Tests for the postinstall hook in mise.toml."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POSTINSTALL = tomllib.loads((REPO_ROOT / "mise.toml").read_text())["hooks"]["postinstall"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def run_postinstall(tmp_path: Path, *, checkout: bool) -> tuple[Path, list[str]]:
    """Run the hook in a fresh directory with a logging uv stub.

    Returns the directory and the uv invocations. Without checkout, the
    directory holds only a copy of mise.toml, like the CI image build's.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    uv = stub_bin / "uv"
    uv.write_text(f'#!/bin/sh\necho "$*" >>{uv_log}\n')
    uv.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        PATH=f"{stub_bin}:{Path(shutil.which('git') or '/usr/bin/git').parent}:/usr/bin:/bin",
        GIT_CEILING_DIRECTORIES=str(tmp_path),
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    shutil.copy(REPO_ROOT / "mise.toml", cwd)
    if checkout:
        subprocess.run(["git", "init", "-q"], cwd=cwd, env=env, check=True)
    # mise runs hooks through its inline shell, `sh -c -o errexit`.
    subprocess.run(["sh", "-ec", POSTINSTALL], cwd=cwd, env=env, check=True)
    return cwd, uv_log.read_text().splitlines() if uv_log.exists() else []


def test_manifest_only_directory_runs_no_bootstrap(tmp_path: Path) -> None:
    cwd, uv_calls = run_postinstall(tmp_path, checkout=False)

    assert uv_calls == []
    assert not (cwd / ".git").exists()


def test_checkout_syncs_dependencies_and_installs_pre_push(tmp_path: Path) -> None:
    cwd, uv_calls = run_postinstall(tmp_path, checkout=True)

    assert uv_calls == [
        "sync --locked --quiet",
        "run --quiet python test/setup_mitogen.py",
    ]
    pre_push = cwd / ".git" / "hooks" / "pre-push"
    assert os.access(pre_push, os.X_OK)
    assert "exec mise run lint" in pre_push.read_text()
