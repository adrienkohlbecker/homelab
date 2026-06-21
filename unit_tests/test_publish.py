"""Unit tests for packer/publish.py — atomic artifact publishing."""

import importlib
import os
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "packer" / "publish.py"


def _load():
    spec = importlib.util.spec_from_file_location("publish", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pub = _load()


class TestAcquireExclusive:
    def test_acquires_unlocked_fd(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "test.lock"
        fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            pub.acquire_exclusive(fd, str(lockfile), 1.0)
        finally:
            os.close(fd)

    def test_timeout_on_held_lock(self, tmp_path: Path) -> None:
        import fcntl

        lockfile = tmp_path / "test.lock"
        fd1 = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd1, fcntl.LOCK_EX)
        fd2 = os.open(str(lockfile), os.O_RDWR, 0o644)
        try:
            with pytest.raises(SystemExit, match="publish-lock held"):
                pub.acquire_exclusive(fd2, str(lockfile), 0.5)
        finally:
            os.close(fd2)
            os.close(fd1)


class TestMainAtomicPublish:
    def test_publishes_new_artifact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("new")
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        dst = artifact_dir / "dst"
        lockfile = tmp_path / ".publish-lock"

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        pub.main()

        assert dst.exists()
        assert (dst / "image.qcow2").read_text() == "new"
        assert not src.exists()
        assert lockfile.exists()

    def test_replaces_existing_artifact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("v2")
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        dst = artifact_dir / "dst"
        dst.mkdir()
        (dst / "image.qcow2").write_text("v1")
        lockfile = tmp_path / ".publish-lock"

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        pub.main()

        assert (dst / "image.qcow2").read_text() == "v2"
        assert not src.exists()
        assert not any(p.name.startswith("dst.outgoing") for p in artifact_dir.iterdir())

    def test_usage_on_bad_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["publish.py"])
        with pytest.raises(SystemExit, match="usage"):
            pub.main()
