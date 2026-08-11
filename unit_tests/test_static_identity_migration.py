import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "roles/static_identity_migration/files/migrate_ownership.py"
SPEC = importlib.util.spec_from_file_location("migrate_ownership", SCRIPT)
assert SPEC
assert SPEC.loader
migrate_ownership = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate_ownership)


def test_migrate_path_changes_only_matching_owners(tmp_path, monkeypatch) -> None:
    matching = tmp_path / "matching"
    untouched = tmp_path / "untouched"
    matching.write_text("matching")
    untouched.write_text("untouched")
    current_uid = os.getuid()
    current_gid = os.getgid()
    calls = []

    def record_chown(path, uid, gid, *, follow_symlinks):
        calls.append((Path(path), uid, gid, follow_symlinks))

    monkeypatch.setattr(migrate_ownership.os, "chown", record_chown)
    changed = migrate_ownership.migrate_path(
        tmp_path,
        {current_uid: 60706},
        {current_gid: 60800},
        dry_run=False,
    )

    assert changed == 3
    assert {call[0] for call in calls} == {tmp_path, matching, untouched}
    assert all(call[1:] == (60706, 60800, False) for call in calls)


def test_migrate_path_ignores_unknown_ids(tmp_path, monkeypatch) -> None:
    path = tmp_path / "untouched"
    path.write_text("untouched")
    calls = []
    monkeypatch.setattr(
        migrate_ownership.os,
        "chown",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    changed = migrate_ownership.migrate_path(
        tmp_path,
        {os.getuid() + 1: 60706},
        {os.getgid() + 1: 60800},
        dry_run=False,
    )

    assert changed == 0
    assert calls == []


def test_migrate_path_ignores_file_removed_before_chown(tmp_path, monkeypatch) -> None:
    path = tmp_path / "removed"
    path.write_text("removed")

    def remove_then_fail(*args, **kwargs):
        path.unlink()
        raise FileNotFoundError

    monkeypatch.setattr(migrate_ownership.os, "chown", remove_then_fail)

    changed = migrate_ownership.migrate_path(
        path,
        {os.getuid(): 60706},
        {},
        dry_run=False,
    )

    assert changed == 0


def test_paths_on_device_ignores_file_removed_after_walk(tmp_path, monkeypatch) -> None:
    removed = tmp_path / "removed"
    removed.write_text("removed")
    real_lstat = Path.lstat

    def lstat(path):
        if path == removed:
            raise FileNotFoundError
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)

    assert [path for path, _ in migrate_ownership.paths_on_device(tmp_path)] == [tmp_path]


def test_validate_required_mounts_rejects_unmounted_path(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="required filesystems are not mounted"):
        migrate_ownership.validate_required_mounts({"required_mounts": [str(tmp_path / "missing")]})


def test_active_identity_uid_labels_uses_current_named_account(monkeypatch) -> None:
    monkeypatch.setattr(
        migrate_ownership.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=958),
    )

    assert migrate_ownership.active_identity_uid_labels(
        {
            "identities": [
                {
                    "name": "gitlab_runner",
                    "uid": 60714,
                    "old_uids": [999],
                }
            ]
        }
    ) == {958: {"gitlab_runner"}}


def test_active_processes_reports_source_uid(tmp_path) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "status").write_text("Name:\tworker\n" "Uid:\t958\t958\t958\t958\n")

    assert migrate_ownership.active_processes({958}, proc_root=tmp_path) == [
        migrate_ownership.ActiveProcess(uid=958, pid=123, name="worker")
    ]
