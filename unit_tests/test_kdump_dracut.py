"""Exercise capture-image installation when the optional size estimator fails."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "roles/kdump/files/kdump_dracut.sh"


@pytest.mark.parametrize("measurement", ["failure", "zero", "valid", "generation_failure"])
def test_capture_image_installation(tmp_path: Path, measurement: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kdump_dir = tmp_path / "kdump"
    kdump_dir.mkdir()
    kernel = "7.0-test"
    target = kdump_dir / f"initrd.img-{kernel}"
    target.write_text("old image")
    size_file = kdump_dir / f"size_initrd.img-{kernel}"
    size_file.write_text("999\n")
    commands = {
        "kdump-config": "exit 0",
        "linux-version": f"printf '%s\\n' {kernel}",
        "ischroot": "exit 1",
        # dracut tries --add-confdir as a path before its configuration
        # directories; the caller's planted directory must not resolve.
        "dracut": '[ ! -d "$3" ] || exit 97\nprintf "new image" >"$4"'
        + ("\nexit 1" if measurement == "generation_failure" else ""),
        "3cpio": {
            "failure": "exit 1",
            "zero": "exit 0",
            "valid": "printf 'archive\\t0\\t0\\t0\\t1048577\\n'",
            "generation_failure": "exit 0",
        }[measurement],
    }
    for name, body in commands.items():
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
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    env.pop("DEB_MAINT_PARAMS", None)
    env.pop("INITRD", None)
    # apt runs the hook from its caller's working directory.
    caller = tmp_path / "caller"
    (caller / "kdump-tools").mkdir(parents=True)
    result = subprocess.run(["bash", str(hook), kernel], cwd=caller, env=env, text=True, capture_output=True)

    if measurement == "generation_failure":
        assert result.returncode != 0
        assert target.read_text() == "old image"
        assert size_file.read_text() == "999\n"
    else:
        assert result.returncode == 0, result.stderr
        assert target.read_text() == "new image"
        if measurement == "valid":
            assert size_file.read_text() == "2\n"
        else:
            assert "estimator may be unavailable" in result.stderr
            assert not size_file.exists()
    assert not Path(f"{target}.new").exists()
