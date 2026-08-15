"""Unit tests for the ZFS restore planner and safety checks."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "roles" / "zfs_autobackup" / "files" / "zfs_backup_restore.py"
_SNAPSHOTS = ["bak-20260801000000", "bak-20260802000000", "bak-20260803000000"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zfs_backup_restore", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through the module registry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


restore = _load()


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


@pytest.fixture
def config():
    return restore.Config(
        "root@lab",
        "apoc/lab/rpool/ROOT/jammy",
        _SNAPSHOTS[0],
        _SNAPSHOTS[-1],
        "rpool/ROOT/jammy",
        "/mnt/zfs_restore_validation/jammy",
    )


class TestParseConfig:
    def test_accepts_shell_safe_arguments(self, config) -> None:
        assert (
            restore.parse_config(
                [
                    config.target_ssh,
                    config.replica_dataset,
                    config.start_suffix,
                    config.end_suffix,
                    config.target_dataset,
                    config.mountpoint,
                ]
            )
            == config
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ["root@lab"],
            ["-oProxyCommand=bad", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt"],
            ["root@lab", "pool/src;bad", *_SNAPSHOTS[:2], "pool/dst", "/mnt"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "relative"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt//bad"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/bad/"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/./bad"],
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/../bad"],
            ["root@lab", "pool/src", "manual", _SNAPSHOTS[1], "pool/dst", "/mnt"],
            ["root@lab", "pool/src", _SNAPSHOTS[1], _SNAPSHOTS[0], "pool/dst", "/mnt"],
        ],
    )
    def test_rejects_invalid_or_shell_unsafe_arguments(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as error:
            restore.parse_config(argv)

        assert error.value.code == 2


class TestBuildPlans:
    def test_maps_the_replica_tree_to_the_target_tree(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        source_child = f"{config.replica_dataset}/var"
        calls = []

        def capture(*args, **kwargs):
            calls.append(args)
            assert args[0] == "list"
            if "snapshot" not in args:
                return _completed(f"{config.replica_dataset}\n{source_child}\n")
            return _completed()

        monkeypatch.setattr(restore, "zfs_capture", capture)

        plans = restore.build_plans(config)

        assert [(plan.source, plan.target) for plan in plans] == [
            (config.replica_dataset, config.target_dataset),
            (source_child, f"{config.target_dataset}/var"),
        ]
        assert calls[-1] == (
            "list",
            "-H",
            "-t",
            "snapshot",
            "-o",
            "name",
            f"{config.replica_dataset}@{config.start_suffix}",
            f"{config.replica_dataset}@{config.end_suffix}",
            f"{source_child}@{config.start_suffix}",
            f"{source_child}@{config.end_suffix}",
        )

    def test_rejects_a_missing_endpoint(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = 0

        def capture(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _completed(config.replica_dataset)
            raise restore.RestoreError("missing")

        monkeypatch.setattr(restore, "zfs_capture", capture)

        with pytest.raises(restore.RestoreError, match="range is incomplete"):
            restore.build_plans(config)


class TestInspectTargets:
    def test_accepts_an_absent_target_tree(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "remote_zfs_capture", lambda *args, **kwargs: _completed())

        restore.inspect_targets(config)

    @pytest.mark.parametrize("existing", ["rpool/ROOT/jammy", "rpool/ROOT/jammy/var"])
    def test_rejects_an_existing_target_or_descendant(
        self, config, existing: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(restore, "remote_zfs_capture", lambda *args, **kwargs: _completed(existing))

        with pytest.raises(restore.RestoreError, match="already exists"):
            restore.inspect_targets(config)


class TestStreaming:
    def test_receive_transfers_bytes_with_an_uncompressed_nonmultiplexed_ssh_stream(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = []
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            commands.append(command)
            if command[0] == "zfs":
                replacement = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'payload')"]
            elif command[0] == "mbuffer":
                replacement = [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ]
            else:
                replacement = [
                    sys.executable,
                    "-c",
                    "import sys; raise SystemExit(sys.stdin.buffer.read() != b'payload')",
                ]
            return real_popen(replacement, **kwargs)

        monkeypatch.setattr(restore.subprocess, "Popen", popen)

        restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

        assert commands == [
            ["zfs", "send", "pool/src@snapshot"],
            ["mbuffer", "-m", "256M"],
            [
                "ssh",
                *restore.SSH_BULK_OPTIONS,
                config.target_ssh,
                "sudo",
                "zfs",
                "recv",
                "-u",
                config.target_dataset,
            ],
        ]

    def test_receive_reports_a_pipeline_member_failure(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            exit_code = 7 if command[0] == "mbuffer" else 0
            return real_popen([sys.executable, "-c", f"raise SystemExit({exit_code})"], **kwargs)

        monkeypatch.setattr(restore.subprocess, "Popen", popen)

        with pytest.raises(restore.RestoreError, match=r"mbuffer -m 256M \(exit 7\)"):
            restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

    def test_receive_wraps_a_missing_executable(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command[0] == "mbuffer":
                raise FileNotFoundError("missing")
            return real_popen([sys.executable, "-c", "pass"], **kwargs)

        monkeypatch.setattr(restore.subprocess, "Popen", popen)

        with pytest.raises(restore.RestoreError, match="could not start restore pipeline command mbuffer"):
            restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

    def test_sync_sends_one_full_and_one_aggregate_incremental(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset)
        received = []
        monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append((command, target)))

        restore.sync_plan(config, plan)

        assert received == [
            (
                ["sudo", "zfs", "send", "-bpcv", f"{plan.source}@{_SNAPSHOTS[0]}"],
                plan.target,
            ),
            (
                [
                    "sudo",
                    "zfs",
                    "send",
                    "-bpcv",
                    "-I",
                    f"@{_SNAPSHOTS[0]}",
                    f"{plan.source}@{_SNAPSHOTS[2]}",
                ],
                plan.target,
            ),
        ]

    def test_sync_sends_only_the_full_stream_for_one_snapshot(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        config = restore.Config(
            config.target_ssh,
            config.replica_dataset,
            _SNAPSHOTS[-1],
            _SNAPSHOTS[-1],
            config.target_dataset,
            config.mountpoint,
        )
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset)
        received = []
        monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append((command, target)))

        restore.sync_plan(config, plan)

        assert received == [
            (
                ["sudo", "zfs", "send", "-bpcv", f"{plan.source}@{_SNAPSHOTS[-1]}"],
                plan.target,
            )
        ]


def test_finalize_mounts_only_eligible_datasets_and_restores_local_tags(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = restore.DatasetPlan(config.replica_dataset, config.target_dataset)
    child = restore.DatasetPlan(f"{config.replica_dataset}/var", f"{config.target_dataset}/var")
    commands = []
    monkeypatch.setattr(restore, "remote_zfs_run", lambda _config, *args: commands.append(args))

    def properties(_config, *args, **kwargs):
        target = args[-1]
        if target == root.target:
            return _completed("mountpoint\tnone\ncanmount\ton\nmounted\tno\nautobackup:bak\tfalse\n")
        return _completed("mountpoint\t/var\ncanmount\ton\nmounted\tno\nautobackup:bak\ttrue\n")

    monkeypatch.setattr(restore, "remote_zfs_capture", properties)

    restore.finalize(config, [root, child])

    assert ("mount", root.target) not in commands
    assert ("mount", child.target) in commands
    assert ("set", "autobackup:bak=true", child.target) in commands
    assert ("set", "autobackup:bak=true", root.target) not in commands
