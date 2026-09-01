"""Unit tests for packer/publish.py — atomic artifact publishing."""

import importlib.util
import os
import stat
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "packer" / "publish.py"


def _load():
    spec = importlib.util.spec_from_file_location("publish", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
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

    def test_prunes_completed_swap_debris(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("v2")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "image.qcow2").write_text("v1")
        outgoing = tmp_path / "dst.outgoing"
        outgoing.mkdir()
        (outgoing / "image.qcow2").write_text("v0")
        lockfile = tmp_path / ".publish-lock"

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        pub.main()

        assert (dst / "image.qcow2").read_text() == "v2"
        assert not outgoing.exists()

    def test_restores_interrupted_swap_before_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("new")
        dst = tmp_path / "dst"
        outgoing = tmp_path / "dst.outgoing"
        outgoing.mkdir()
        (outgoing / "image.qcow2").write_text("old")
        lockfile = tmp_path / ".publish-lock"
        real_replace = os.replace

        def fail_new_swap(source: str, destination: str) -> None:
            if source == str(src) and destination == str(dst):
                raise OSError("swap failed")
            real_replace(source, destination)

        monkeypatch.setattr(pub.os, "replace", fail_new_swap)
        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])

        with pytest.raises(OSError, match="swap failed"):
            pub.main()

        assert (dst / "image.qcow2").read_text() == "old"
        assert not outgoing.exists()
        assert (src / "image.qcow2").read_text() == "new"

    def test_uses_existing_read_only_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("new")
        dst = tmp_path / "dst"
        lockfile = tmp_path / ".publish-lock"
        lockfile.touch(mode=0o444)

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        pub.main()

        assert (dst / "image.qcow2").read_text() == "new"

    def test_created_lock_excludes_others(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "image.qcow2").write_text("new")
        dst = tmp_path / "dst"
        lockfile = tmp_path / ".publish-lock"

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        # flock only needs a readable fd, so a world-readable lockfile would let
        # any local account take LOCK_EX; pin the umask so the assertion is
        # about the requested creation mode, not the environment.
        old_umask = os.umask(0o022)
        try:
            pub.main()
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(lockfile.stat().st_mode) == 0o640

    def test_grants_group_access_to_published_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src"
        nested = src / "nested"
        nested.mkdir(parents=True, mode=0o750)
        dst = tmp_path / "dst"
        lockfile = tmp_path / ".publish-lock"

        monkeypatch.setattr("sys.argv", ["publish.py", str(lockfile), str(src), str(dst)])
        pub.main()

        for directory in (dst, dst / "nested"):
            assert stat.S_IMODE(directory.stat().st_mode) & stat.S_IRWXG == stat.S_IRWXG

    def test_usage_on_bad_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["publish.py"])
        with pytest.raises(SystemExit, match="usage"):
            pub.main()
