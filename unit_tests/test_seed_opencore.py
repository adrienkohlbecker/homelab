"""Protect OpenCore seed publication and unrelated guest identity data."""

import fcntl
import importlib.util
import plistlib
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "seed_opencore", Path(__file__).resolve().parents[1] / "roles/macos_vm/files/seed_opencore.py"
)
assert _SPEC
assert _SPEC.loader
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY])
def test_patch_preserves_unrelated_identity_and_plist_format(tmp_path, fmt):
    config = {
        "PlatformInfo": {"Generic": {"SystemSerialNumber": "test-only-serial"}},
        "NVRAM": {"Add": {seed.APPLE_BOOT_GUID: {"unrelated": b"\x00\xff", "boot-args": "old"}}},
    }
    path = tmp_path / "config.plist"
    path.write_bytes(plistlib.dumps(config, fmt=fmt))
    seed.patch_config(path, "en-US:1", "en_FR", "keepsyms=1 -v")
    result = plistlib.loads(path.read_bytes())
    assert result["PlatformInfo"] == config["PlatformInfo"]
    assert result["NVRAM"]["Add"][seed.APPLE_BOOT_GUID] == {
        "unrelated": b"\x00\xff",
        "AppleLocale": "en_FR",
        "boot-args": "keepsyms=1 -v",
        "prev-lang:kbd": b"en-US:1",
        "#INFO (prev-lang:kbd)": "en-US:1 (language:keyboard layout ID)",
    }
    assert path.read_bytes().startswith(b"bplist00") == (fmt == plistlib.FMT_BINARY)


def test_malformed_plist_is_not_overwritten(tmp_path):
    path = tmp_path / "config.plist"
    path.write_bytes(b"not a plist")
    with pytest.raises(plistlib.InvalidFileException):
        seed.patch_config(path, "en-US:1", "en_FR", "-v")
    assert path.read_bytes() == b"not a plist"


@pytest.fixture
def destination(tmp_path, monkeypatch):
    monkeypatch.setattr(seed.shutil, "chown", lambda *args, **kwargs: None)
    return tmp_path / "OpenCore.qcow2"


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_destination_is_never_rebuilt(destination, monkeypatch, symlink):
    if symlink:
        destination.symlink_to(destination.parent / "missing")
    else:
        destination.write_bytes(b"existing guest identity")
    monkeypatch.setattr(seed, "build_image", lambda *args: pytest.fail("existing image must not be rebuilt"))
    assert seed.seed_image(Path("missing source"), destination, "en-US:1", "en_FR", "-v") is False
    if symlink:
        assert destination.is_symlink()
    else:
        assert destination.read_bytes() == b"existing guest identity"


def test_interrupted_build_can_retry_without_publishing_partial_image(destination, monkeypatch):
    def interrupted(source, image, *args):
        image.write_bytes(b"partial")
        raise subprocess.TimeoutExpired("qemu-img", 120)

    monkeypatch.setattr(seed, "build_image", interrupted)
    with pytest.raises(subprocess.TimeoutExpired):
        seed.seed_image(Path("source"), destination, "en-US:1", "en_FR", "-v")
    assert not destination.exists()
    assert not list(destination.parent.glob(".opencore_*"))
    lock = destination.with_suffix(".qcow2.lock")
    inode = lock.stat().st_ino
    monkeypatch.setattr(seed, "build_image", lambda source, image, *args: image.write_bytes(b"complete"))
    assert seed.seed_image(Path("source"), destination, "en-US:1", "en_FR", "-v") is True
    assert destination.read_bytes() == b"complete"
    assert lock.stat().st_ino == inode


def test_concurrent_builder_is_rejected(destination, monkeypatch):
    monkeypatch.setattr(seed, "build_image", lambda *args: pytest.fail("another builder owns the lock"))
    with destination.with_suffix(".qcow2.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            seed.seed_image(Path("source"), destination, "en-US:1", "en_FR", "-v")
    assert not destination.exists()


def test_destination_created_outside_lock_is_not_overwritten(destination, monkeypatch):
    def race(source, image, *args):
        image.write_bytes(b"new seed")
        destination.write_bytes(b"other writer")

    monkeypatch.setattr(seed, "build_image", race)
    with pytest.raises(FileExistsError):
        seed.seed_image(Path("source"), destination, "en-US:1", "en_FR", "-v")
    assert destination.read_bytes() == b"other writer"
