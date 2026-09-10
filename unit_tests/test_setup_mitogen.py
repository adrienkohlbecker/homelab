"""Regression tests for the test harness Mitogen strategy symlink."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ansible_mitogen
import pytest
from conftest import load_repo_module

setup_mitogen = load_repo_module("test/setup_mitogen.py")


def test_parallel_repair_uses_an_atomic_symlink_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / setup_mitogen.SYMLINK_NAME
    old_target = tmp_path / "old_strategy"
    old_target.mkdir()
    link.symlink_to(old_target)

    worker_count = 8
    read_barrier = threading.Barrier(worker_count)
    original_readlink = os.readlink

    def synchronized_readlink(path: os.PathLike[str] | str) -> str:
        if Path(path) == link:
            read_barrier.wait()
        return original_readlink(path)

    monkeypatch.setattr(setup_mitogen.os, "readlink", synchronized_readlink)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        repaired = list(pool.map(lambda _: setup_mitogen.ensure_mitogen_symlink(tmp_path), range(worker_count)))

    expected = Path(ansible_mitogen.__file__).resolve().parent / "plugins" / "strategy"
    assert repaired == [link] * worker_count
    assert original_readlink(link) == str(expected)
    assert not list(tmp_path.glob(f".{link.name}.*"))
