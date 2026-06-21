"""Unit tests for the promoted.json pointer shared by the qemu-image tasks.

The pointer object replaces the old SSM parameter as the live-build selector.
These cover the producer-side format (upload-s3.py ``pointer_body``) and the
consumer-side validation (hydrate-qemu-images.py ``resolve_build_id``), since
both must agree on the same JSON shape for S3.

The task scripts have hyphenated filenames, so they are loaded via
importlib.util.spec_from_file_location rather than a plain import. Loading is
side-effect-free: both modules do their work under ``if __name__ == "__main__"``.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_TASKS = Path(__file__).resolve().parent.parent / "mise-tasks"
_UPLOAD_PATH = _TASKS / "packer" / "upload-s3.py"
_HYDRATE_PATH = _TASKS / "ci" / "hydrate-qemu-images.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve string annotations
    # (the modules use `from __future__ import annotations`).
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


upload = _load("upload_s3", _UPLOAD_PATH)
hydrate = _load("hydrate_qemu_images", _HYDRATE_PATH)


def _args(**overrides: object) -> argparse.Namespace:
    base = {"build_id": "ci-42-gdeadbeef0000", "machine": "box", "ubuntu": "jammy"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestPointerBody:
    def test_format_is_sorted_indented_trailing_newline(self) -> None:
        body = upload.pointer_body(_args())
        assert body.endswith("\n")
        # sort_keys=True, indent=2
        assert body == (
            "{\n" '  "build_id": "ci-42-gdeadbeef0000",\n' '  "machine": "box",\n' '  "ubuntu": "jammy"\n' "}\n"
        )

    def test_round_trip(self) -> None:
        body = upload.pointer_body(_args(build_id="b1", machine="box_deps", ubuntu="noble"))
        assert json.loads(body) == {"build_id": "b1", "machine": "box_deps", "ubuntu": "noble"}

    def test_pointer_name_constant_matches(self) -> None:
        assert upload.POINTER_NAME == "promoted.json"
        assert hydrate.POINTER_NAME == "promoted.json"


class TestResolveBuildId:
    def _resolve(self, monkeypatch: pytest.MonkeyPatch, body: str, **arg_overrides: object) -> str:
        monkeypatch.setattr(hydrate, "output", lambda argv, **kw: body)
        base = {"machine": "box", "ubuntu": "jammy"}
        base.update(arg_overrides)
        args = argparse.Namespace(**base)
        return hydrate.resolve_build_id(args)

    def test_reads_build_id_from_pointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = upload.pointer_body(_args(build_id="ci-7-gabc", machine="box", ubuntu="jammy"))
        assert self._resolve(monkeypatch, body) == "ci-7-gabc"

    def test_machine_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = upload.pointer_body(_args(machine="box_deps"))
        with pytest.raises(SystemExit, match="machine mismatch"):
            self._resolve(monkeypatch, body, machine="box")

    def test_ubuntu_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = upload.pointer_body(_args(ubuntu="noble"))
        with pytest.raises(SystemExit, match="ubuntu mismatch"):
            self._resolve(monkeypatch, body, ubuntu="jammy")

    def test_empty_pointer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit, match="missing or empty"):
            self._resolve(monkeypatch, "")
