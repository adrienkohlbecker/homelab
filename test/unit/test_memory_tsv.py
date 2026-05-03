"""Tests for the memory.tsv read/write/upsert path.

The module-level paths (MEMORY_TSV, MEMORY_TSV_LOCK, OUT_DIR) are resolved
at import time, so each test redirects all three onto tmp_path before
exercising the helpers.
"""

import threading
from pathlib import Path

import pytest

import machine


@pytest.fixture
def out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point MEMORY_TSV / MEMORY_TSV_LOCK / OUT_DIR at tmp_path for one test.

    Pre-creates OUT_DIR so callers can invoke _write_memory_rows directly;
    in production the mkdir lives in _memory_tsv_lock, which the upsert path
    always goes through.
    """
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(machine, "OUT_DIR", out)
    monkeypatch.setattr(machine, "MEMORY_TSV", out / "memory.tsv")
    monkeypatch.setattr(machine, "MEMORY_TSV_LOCK", out / "memory.tsv.lock")
    return out


@pytest.mark.usefixtures("out_dir")
def test_read_returns_empty_dict_when_file_missing() -> None:
    assert machine._read_memory_rows() == {}


@pytest.mark.usefixtures("out_dir")
def test_write_then_read_round_trip() -> None:
    rows = {
        ("rolea", "jammy", "container"): 12345,
        ("roleb", "noble", "box"): 67890,
    }
    machine._write_memory_rows(rows)
    assert machine._read_memory_rows() == rows


def test_write_emits_header_and_sorts_keys(out_dir: Path) -> None:
    machine._write_memory_rows(
        {
            ("zeta", "noble", "lab"): 100,
            ("alpha", "jammy", "container"): 200,
        }
    )
    lines = (out_dir / "memory.tsv").read_text().splitlines()
    assert lines[0] == "Role\tUbuntu\tMachine\tPeakKB"
    # Sorted by tuple -> alpha row precedes zeta
    assert lines[1].startswith("alpha\t")
    assert lines[2].startswith("zeta\t")


def test_write_is_atomic_no_tmp_left_behind(out_dir: Path) -> None:
    machine._write_memory_rows({("r", "u", "m"): 1})
    assert (out_dir / "memory.tsv").exists()
    # The .tmp sibling exists only briefly during write; after replace() it
    # must be gone so a concurrent reader never sees a half-written file.
    assert not (out_dir / "memory.tsv.tmp").exists()


@pytest.mark.usefixtures("out_dir")
def test_upsert_inserts_into_empty_file() -> None:
    machine.upsert_memory_row("rolex", "jammy", "container", 5000)
    assert machine._read_memory_rows() == {("rolex", "jammy", "container"): 5000}


@pytest.mark.usefixtures("out_dir")
def test_upsert_overwrites_same_key() -> None:
    machine.upsert_memory_row("rolex", "jammy", "container", 1000)
    machine.upsert_memory_row("rolex", "jammy", "container", 9999)
    assert machine._read_memory_rows() == {("rolex", "jammy", "container"): 9999}


@pytest.mark.usefixtures("out_dir")
def test_upsert_preserves_other_rows() -> None:
    machine.upsert_memory_row("a", "jammy", "container", 1)
    machine.upsert_memory_row("b", "noble", "box", 2)
    assert machine._read_memory_rows() == {
        ("a", "jammy", "container"): 1,
        ("b", "noble", "box"): 2,
    }


@pytest.mark.usefixtures("out_dir")
def test_concurrent_upserts_do_not_lose_updates() -> None:
    """Without flock the read-modify-write loop would lose rows under contention.

    20 threads each upsert a distinct key; the final TSV must contain every
    row. flock running per-fd in the same process is enough to demonstrate
    the serialisation -- the production case (separate testrole.py
    subprocesses) uses the same primitive.
    """
    n = 20

    def worker(i: int) -> None:
        machine.upsert_memory_row(f"role{i:02d}", "jammy", "container", i * 100)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = machine._read_memory_rows()
    assert len(rows) == n
    for i in range(n):
        assert rows[(f"role{i:02d}", "jammy", "container")] == i * 100
