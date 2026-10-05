"""Exercise the Dracut capture-image hook with command doubles."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "roles/kdump/files/kdump_dracut.sh"
KERNEL = "7.0-test"


@dataclass
class Hook:
    """The production hook staged against command doubles and a scratch kdump dir."""

    path: Path
    bin_dir: Path
    target: Path

    def run(self, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PATH=f"{self.bin_dir}:{os.environ['PATH']}")
        env.pop("DEB_MAINT_PARAMS", None)
        env.pop("INITRD", None)
        env.update(env_extra)
        # apt runs the hook from its caller's working directory.
        caller = self.path.parent / "caller"
        (caller / "kdump-tools").mkdir(parents=True, exist_ok=True)
        return subprocess.run(["bash", str(self.path), KERNEL], cwd=caller, env=env, text=True, capture_output=True)

    @property
    def dracut_ran(self) -> bool:
        return (self.bin_dir / "dracut.ran").exists()


def _hook(tmp_path: Path, **bodies: str | None) -> Hook:
    """Stage the hook; each body replaces a command double, and None omits it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kdump_dir = tmp_path / "kdump"
    kdump_dir.mkdir()
    target = kdump_dir / f"initrd.img-{KERNEL}"
    target.write_text("old image")
    commands: dict[str, str | None] = {
        "kdump-config": "exit 0",
        "linux-version": f"printf '%s\\n' {KERNEL}",
        "ischroot": "exit 1",
        # dracut tries --add-confdir as a path before its configuration
        # directories; the caller's planted directory must not resolve.
        "dracut": '[ ! -d "$3" ] || exit 97\ntouch "$0.ran"\nprintf "new image" >"$4"',
    } | bodies
    for name, body in commands.items():
        if body is None:
            continue
        executable = bin_dir / name
        executable.write_text(f"#!/bin/bash\nset -euo pipefail\n{body}\n")
        executable.chmod(0o755)

    # Redirect only system paths; run the production hook with command doubles.
    hook = tmp_path / "hook.sh"
    hook.write_text(
        HOOK.read_text()
        .replace("/usr/sbin/kdump-config", str(bin_dir / "kdump-config"))
        .replace("/var/lib/kdump", str(kdump_dir))
    )
    return Hook(hook, bin_dir, target)


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="direct"),
        pytest.param({"DEB_MAINT_PARAMS": "'configure' ''"}, id="configure_action"),
    ],
)
def test_capture_image_installation(tmp_path: Path, env: dict[str, str]) -> None:
    hook = _hook(tmp_path)

    result = hook.run(**env)

    assert result.returncode == 0, result.stderr
    assert hook.dracut_ran
    assert hook.target.read_text() == "new image"
    assert not Path(f"{hook.target}.new").exists()


def test_generation_failure_keeps_the_installed_image(tmp_path: Path) -> None:
    hook = _hook(tmp_path, dracut='printf "new image" >"$4"\nexit 1')

    result = hook.run()

    assert result.returncode != 0
    assert hook.target.read_text() == "old image"
    assert not Path(f"{hook.target}.new").exists()


@pytest.mark.parametrize(
    ("bodies", "env"),
    [
        pytest.param({"kdump-config": None}, {}, id="no_kdump_config"),
        pytest.param({"linux-version": "printf 'other\\n'"}, {}, id="unknown_kernel"),
        pytest.param({}, {"INITRD": "No"}, id="initrd_disabled"),
        pytest.param({"ischroot": "exit 0"}, {}, id="chroot"),
        pytest.param({}, {"DEB_MAINT_PARAMS": f"'remove' '{KERNEL}'"}, id="remove_action"),
    ],
)
def test_hook_skips_without_building(tmp_path: Path, bodies: dict[str, str | None], env: dict[str, str]) -> None:
    hook = _hook(tmp_path, **bodies)

    result = hook.run(**env)

    assert result.returncode == 0, result.stderr
    assert not hook.dracut_ran
    assert hook.target.read_text() == "old image"
