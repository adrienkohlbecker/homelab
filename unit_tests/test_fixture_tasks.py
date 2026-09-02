"""Behavioral tests for test-fixture mise task wrappers."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_BOX_DEPS_SH = REPO_ROOT / "mise-tasks" / "test" / "build_box_deps.sh"


def _executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def test_build_box_deps_runs_once_per_ubuntu(tmp_path: Path) -> None:
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
    env = dict(os.environ)
    env.update(
        HOMELAB_CI_DIR=str(tmp_path / "homelab_ci"),
        usage_ubuntu="noble resolute",
    )
    env["LAUNCH_TEST_LOG"] = str(launch_log)
    for ubuntu in ("noble", "resolute"):
        source = Path(env["HOMELAB_CI_DIR"]) / ubuntu / "box"
        source.mkdir(parents=True)
        (source / "artifact").write_text("source\n")

    result = subprocess.run(
        ["bash", str(BUILD_BOX_DEPS_SH)],
        cwd=worktree,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = launch_log.read_text().splitlines()
    assert len(calls) == 2
    for ubuntu, call in zip(("noble", "resolute"), calls, strict=True):
        args = shlex.split(call)
        image_dir = Path(args[args.index("--image-dir") + 1])
        destination = Path(env["HOMELAB_CI_DIR"]) / ubuntu / "box_deps"
        assert f"--ubuntu {ubuntu}" in call
        assert "--seed test/playbooks/build_box_deps.yml" in call
        assert image_dir.parent == Path(env["HOMELAB_CI_DIR"])
        assert image_dir.name.startswith(f".build-box-deps-{ubuntu}-")
        assert (destination / "artifact").read_text() == "source\n"
        assert (destination / "seeded").read_text() == "yes\n"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o2770
