"""Pure-function tests for extract._qcow2_fingerprint."""

from pathlib import Path

import extract


def test_stable_for_identical_bytes(tmp_path: Path) -> None:
    a = tmp_path / "a.qcow2"
    a.write_bytes(b"hello world")
    assert extract._qcow2_fingerprint([a]) == extract._qcow2_fingerprint([a])


def test_changes_when_content_changes(tmp_path: Path) -> None:
    a = tmp_path / "a.qcow2"
    a.write_bytes(b"first")
    digest1 = extract._qcow2_fingerprint([a])
    a.write_bytes(b"second")
    digest2 = extract._qcow2_fingerprint([a])
    assert digest1 != digest2


def test_order_independent_across_paths(tmp_path: Path) -> None:
    a = tmp_path / "a.qcow2"
    b = tmp_path / "b.qcow2"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    # Caller-provided ordering must not affect the digest -- the function
    # sorts internally so a multi-disk variant (ubuntu-zfs-lab's 3-way
    # mirror) produces a stable cache key regardless of iteration order.
    assert extract._qcow2_fingerprint([a, b]) == extract._qcow2_fingerprint([b, a])


def test_distinct_for_different_files_same_total_size(tmp_path: Path) -> None:
    a = tmp_path / "a.qcow2"
    b = tmp_path / "b.qcow2"
    # Same byte count, different content -- guards against a hypothetical
    # implementation that only hashed lengths.
    a.write_bytes(b"X" * 16)
    b.write_bytes(b"Y" * 16)
    assert extract._qcow2_fingerprint([a]) != extract._qcow2_fingerprint([b])


def test_handles_chunk_boundary(tmp_path: Path) -> None:
    # _qcow2_fingerprint reads in 1 MiB chunks; ensure a file straddling
    # that boundary hashes correctly (compare to a single-shot hashlib).
    import hashlib

    a = tmp_path / "big.qcow2"
    payload = b"A" * (1024 * 1024 + 17)
    a.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert extract._qcow2_fingerprint([a]) == expected
