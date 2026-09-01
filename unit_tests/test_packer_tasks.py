"""Behavioral tests for the Packer mise task wrappers."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SH = REPO_ROOT / "mise-tasks" / "packer" / "build.sh"
SEED_DEPS_SH = REPO_ROOT / "mise-tasks" / "packer" / "seed-deps.sh"
HETZNER_RESCUE_SH = REPO_ROOT / "mise-tasks" / "packer" / "_hetzner_rescue.sh"
QEMU_HOST_PROVISION_SH = REPO_ROOT / "packer" / "aws" / "files" / "provision_qemu_host.sh"


def _executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _environment(tmp_path: Path, ubuntus: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        HOMELAB_CI_DIR=str(tmp_path / "homelab_ci"),
        usage_no_publish="true",
        usage_sources="box",
        usage_ubuntu=ubuntus,
        usage_upstream="false",
    )
    return env


@pytest.mark.parametrize("ubuntus", ["noble", "noble resolute"])
def test_build_runs_once_per_ubuntu(tmp_path: Path, ubuntus: str) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "packer.log"
    archive_log = tmp_path / "archive.log"
    cache_log = tmp_path / "cache.log"
    _executable(fake_bin / "uname", "#!/bin/sh\nset -eu\nprintf 'Linux\\n'\n")
    _executable(
        fake_bin / "packer",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >>"$PACKER_TEST_LOG"\n'
        'printf "%s\\n" "$PACKER_CACHE_DIR" >>"$PACKER_CACHE_LOG"\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in build_directory=*) build_directory=${arg#*=} ;; esac\n'
        "done\n"
        'tar -tf "$build_directory/homelab-source.tar" >"$PACKER_ARCHIVE_LOG"\n',
    )
    env = _environment(tmp_path, ubuntus)
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        PACKER_ARCHIVE_LOG=str(archive_log),
        PACKER_CACHE_LOG=str(cache_log),
        PACKER_TEST_LOG=str(log),
    )

    result = subprocess.run(["bash", str(BUILD_SH)], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == len(ubuntus.split())
    for ubuntu, call in zip(ubuntus.split(), calls, strict=True):
        assert f"ubuntu_name={ubuntu}" in call
        assert f"output_directory={env['HOMELAB_CI_DIR']}/{ubuntu}" in call
        assert "-only=qemu.box" in call
    assert cache_log.read_text().splitlines() == [f"{env['HOMELAB_CI_DIR']}/packer_cache"] * len(ubuntus.split())

    archive_entries = archive_log.read_text().splitlines()
    assert "roles/refind/files/zz-stage-efi-stub" in archive_entries
    assert "roles/console/files/console-setup" in archive_entries
    assert "roles/console/files/keyboard" in archive_entries
    assert "roles/boot/files/modules_most" in archive_entries
    assert not any(entry == ".git" or entry.startswith(".git/") for entry in archive_entries)
    assert not any(entry == "notes" or entry.startswith("notes/") for entry in archive_entries)
    assert not any(
        entry == "roles/homeassistant/files/ha_gui_config"
        or entry.startswith("roles/homeassistant/files/ha_gui_config/")
        for entry in archive_entries
    )


def test_seed_deps_runs_once_per_ubuntu(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    launch_log = tmp_path / "launch.log"
    _executable(
        worktree / "test" / "launch.py",
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "image_dir = Path(args[args.index('--image-dir') + 1])\n"
        "(image_dir / 'seeded').write_text('yes\\n')\n"
        "with Path(os.environ['LAUNCH_TEST_LOG']).open('a') as log:\n"
        "    log.write(' '.join(args) + '\\n')\n",
    )
    _executable(
        worktree / "packer" / "publish.py",
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "from pathlib import Path\n"
        "src, dst = map(Path, sys.argv[-2:])\n"
        "if dst.exists():\n"
        "    shutil.rmtree(dst)\n"
        "os.replace(src, dst)\n",
    )
    env = _environment(tmp_path, "noble resolute")
    env["LAUNCH_TEST_LOG"] = str(launch_log)
    for ubuntu in ("noble", "resolute"):
        source = Path(env["HOMELAB_CI_DIR"]) / ubuntu / "box"
        source.mkdir(parents=True)
        (source / "artifact").write_text("source\n")

    result = subprocess.run(["bash", str(SEED_DEPS_SH)], cwd=worktree, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    calls = launch_log.read_text().splitlines()
    assert len(calls) == 2
    for ubuntu, call in zip(("noble", "resolute"), calls, strict=True):
        args = shlex.split(call)
        image_dir = Path(args[args.index("--image-dir") + 1])
        destination = Path(env["HOMELAB_CI_DIR"]) / ubuntu / "box_deps"
        assert f"--ubuntu {ubuntu}" in call
        assert image_dir.parent == Path(env["HOMELAB_CI_DIR"])
        assert image_dir.name.startswith(f".build-seed-{ubuntu}-")
        assert (destination / "artifact").read_text() == "source\n"
        assert (destination / "seeded").read_text() == "yes\n"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o2770


def test_qemu_host_uses_canonical_mise_upstream() -> None:
    provision = QEMU_HOST_PROVISION_SH.read_text()

    assert "https://mise.en.dev/gpg-key.pub" in provision
    assert "https://mise.en.dev/deb stable main" in provision
    assert "mise.jdx.dev" not in provision


def test_hetzner_bulk_ssh_isolates_the_compressed_stream(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    log = tmp_path / "ssh.log"
    _executable(
        fake_bin / "ssh",
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '<call>\\n' >>\"$SSH_TEST_LOG\"\n"
        'printf \'%s\\n\' "$@" >>"$SSH_TEST_LOG"\n',
    )
    env = dict(os.environ)
    env.update(PATH=f"{fake_bin}:{env['PATH']}", SSH_TEST_LOG=str(log))
    script = f"""
set -euo pipefail
source {shlex.quote(str(HETZNER_RESCUE_SH))}
KEY=/tmp/test_key
KNOWN=/tmp/test_known_hosts
RESCUE_IP=192.0.2.1
ssh_rescue true
ssh_rescue_bulk receive-image
"""

    result = subprocess.run(["bash", "-c", script], cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    calls = [call.splitlines() for call in log.read_text().split("<call>\n") if call]
    assert len(calls) == 2
    control, bulk = calls
    # The bulk variant alone must bypass the user ssh config and prefer the
    # hardware AES cipher, with the remote command still last.
    assert "-F" not in control
    assert bulk[bulk.index("-F") + 1] == "none"
    assert "Ciphers=^aes128-gcm@openssh.com" in bulk
    assert bulk[-2:] == ["root@192.0.2.1", "receive-image"]
