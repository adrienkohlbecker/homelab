"""Unit tests for the resumable ZFS restore planner and safety checks."""

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
        "/",
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
            ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/../bad"],
            ["root@lab", "pool/src", "manual", _SNAPSHOTS[1], "pool/dst", "/mnt"],
        ],
    )
    def test_rejects_invalid_or_shell_unsafe_arguments(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as error:
            restore.parse_config(argv)

        assert error.value.code == 2


class TestSelectedSnapshots:
    def test_returns_inclusive_range_in_creation_order(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "\n".join(
            [
                f"{config.replica_dataset}@bak-20260731000000",
                f"{config.replica_dataset}@{_SNAPSHOTS[0]}",
                f"{config.replica_dataset}/child@{_SNAPSHOTS[0]}",
                f"{config.replica_dataset}@{_SNAPSHOTS[1]}",
                f"{config.replica_dataset}@{_SNAPSHOTS[2]}",
                f"{config.replica_dataset}@bak-20260804000000",
            ]
        )
        monkeypatch.setattr(restore, "zfs_capture", lambda *args, **kwargs: _completed(output))

        assert restore.selected_snapshots(config, config.replica_dataset) == _SNAPSHOTS

    def test_rejects_missing_or_reversed_ranges(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "\n".join(f"{config.replica_dataset}@{suffix}" for suffix in reversed(_SNAPSHOTS))
        monkeypatch.setattr(restore, "zfs_capture", lambda *args, **kwargs: _completed(output))

        with pytest.raises(restore.RestoreError, match="newer than"):
            restore.selected_snapshots(config, config.replica_dataset)

        config = restore.Config(
            config.target_ssh,
            config.replica_dataset,
            "bak-20260701000000",
            config.end_suffix,
            config.target_dataset,
            config.mountpoint,
        )
        with pytest.raises(restore.RestoreError, match="range is incomplete"):
            restore.selected_snapshots(config, config.replica_dataset)


class TestBuildPlans:
    def test_maps_the_replica_tree_to_the_target_tree(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        source_child = f"{config.replica_dataset}/var"
        monkeypatch.setattr(
            restore,
            "zfs_capture",
            lambda *args, **kwargs: _completed(f"{config.replica_dataset}\n{source_child}\n"),
        )
        monkeypatch.setattr(restore, "zfs_value", lambda *args: "-")
        monkeypatch.setattr(restore, "selected_snapshots", lambda _config, _dataset: _SNAPSHOTS)

        plans = restore.build_plans(config)

        assert [(plan.source, plan.target) for plan in plans] == [
            (config.replica_dataset, config.target_dataset),
            (source_child, f"{config.target_dataset}/var"),
        ]
        assert all(plan.snapshots == _SNAPSHOTS for plan in plans)

    def test_rejects_clone_datasets(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "zfs_capture", lambda *args, **kwargs: _completed(config.replica_dataset))
        monkeypatch.setattr(restore, "zfs_value", lambda *args: "pool/origin@snapshot")

        with pytest.raises(restore.RestoreError, match="clone datasets are not supported"):
            restore.build_plans(config)


class TestResumeToken:
    def test_extracts_the_encoded_destination_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            restore,
            "zfs_capture",
            lambda *args, **kwargs: _completed(f"resume object = 1\ntoname = pool/target@{_SNAPSHOTS[1]}\n"),
        )

        assert restore.token_suffix("token_1") == _SNAPSHOTS[1]

    def test_rejects_a_preview_without_a_destination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "zfs_capture", lambda *args, **kwargs: _completed("resume object = 1\n"))

        with pytest.raises(restore.RestoreError, match="did not identify a snapshot"):
            restore.token_suffix("token_1")


class TestInspectTargets:
    @staticmethod
    def _patch_state(
        monkeypatch: pytest.MonkeyPatch,
        *,
        datasets: list[str],
        rows: dict[str, list[tuple[str, str]]] | None = None,
        tokens: dict[str, str] | None = None,
        token_suffix: str | None = None,
    ) -> None:
        rows = rows or {}
        tokens = tokens or {}
        monkeypatch.setattr(
            restore,
            "remote_zfs_capture",
            lambda *args, **kwargs: _completed("\n".join(datasets)),
        )
        monkeypatch.setattr(restore, "remote_snapshot_rows", lambda _config, target: rows.get(target, []))
        monkeypatch.setattr(restore, "remote_zfs_value", lambda _config, *args: tokens.get(args[-1], "-"))
        monkeypatch.setattr(restore, "snapshot_guid", lambda _dataset, suffix: f"guid-{suffix}")
        if token_suffix is not None:
            monkeypatch.setattr(restore, "token_suffix", lambda _token: token_suffix)

    def test_leaves_a_missing_target_ready_for_a_full_send(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(monkeypatch, datasets=[])

        restore.inspect_targets(config, [plan])

        assert plan.received_count == 0
        assert plan.resume_token is None

    def test_attaches_a_guid_matching_prefix_and_resume_token(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(
            monkeypatch,
            datasets=[plan.target],
            rows={plan.target: [(_SNAPSHOTS[0], f"guid-{_SNAPSHOTS[0]}")]},
            tokens={plan.target: "token_1"},
            token_suffix=_SNAPSHOTS[1],
        )

        restore.inspect_targets(config, [plan])

        assert plan.received_count == 1
        assert plan.resume_token == "token_1"

    @pytest.mark.parametrize(
        ("rows", "message"),
        [
            ([(_SNAPSHOTS[1], f"guid-{_SNAPSHOTS[1]}")], "not a prefix"),
            ([(_SNAPSHOTS[0], "different-guid")], "GUID mismatch"),
        ],
    )
    def test_rejects_a_nonmatching_target_history(
        self,
        config,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[tuple[str, str]],
        message: str,
    ) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(monkeypatch, datasets=[plan.target], rows={plan.target: rows})

        with pytest.raises(restore.RestoreError, match=message):
            restore.inspect_targets(config, [plan])

    def test_rejects_unexpected_descendants(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(monkeypatch, datasets=[f"{plan.target}/foreign"])

        with pytest.raises(restore.RestoreError, match="unexpected dataset"):
            restore.inspect_targets(config, [plan])

    def test_rejects_an_unattributable_empty_dataset(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(monkeypatch, datasets=[plan.target])

        with pytest.raises(restore.RestoreError, match="without requested snapshots"):
            restore.inspect_targets(config, [plan])

    def test_refuses_to_overwrite_a_completed_target(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        rows = [(suffix, f"guid-{suffix}") for suffix in _SNAPSHOTS]
        self._patch_state(monkeypatch, datasets=[plan.target], rows={plan.target: rows})

        with pytest.raises(restore.RestoreError, match="already contains"):
            restore.inspect_targets(config, [plan])


class TestStreaming:
    def test_pipeline_transfers_bytes(self) -> None:
        restore.pipe_commands(
            [
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'payload')"],
                [
                    sys.executable,
                    "-c",
                    "import sys; raise SystemExit(sys.stdin.buffer.read() != b'payload')",
                ],
            ]
        )

    def test_pipeline_reports_a_member_failure(self) -> None:
        with pytest.raises(restore.RestoreError, match=r"exit 7"):
            restore.pipe_commands([[sys.executable, "-c", "raise SystemExit(7)"]])

    def test_receive_builds_an_uncompressed_nonmultiplexed_ssh_stream(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipelines = []
        monkeypatch.setattr(restore, "pipe_commands", pipelines.append)

        restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

        assert pipelines == [
            [
                ["zfs", "send", "pool/src@snapshot"],
                ["mbuffer", "-m", "256M"],
                [
                    "ssh",
                    *restore.SSH_BULK_OPTIONS,
                    config.target_ssh,
                    "sudo",
                    "zfs",
                    "recv",
                    "-su",
                    config.target_dataset,
                ],
            ]
        ]
        assert "-n" not in pipelines[0][-1]

    def test_sync_resumes_then_sends_the_remaining_incremental(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(
            config.replica_dataset,
            config.target_dataset,
            _SNAPSHOTS,
            received_count=1,
            resume_token="token_1",
        )
        received = []
        monkeypatch.setattr(restore, "trace", lambda _command: None)
        monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append((command, target)))

        restore.sync_plan(config, plan)

        assert received == [
            (["sudo", "zfs", "send", "-t", "token_1"], plan.target),
            (
                [
                    "sudo",
                    "zfs",
                    "send",
                    "-bpcv",
                    "-i",
                    f"@{_SNAPSHOTS[1]}",
                    f"{plan.source}@{_SNAPSHOTS[2]}",
                ],
                plan.target,
            ),
        ]


def test_finalize_mounts_only_eligible_datasets_and_restores_local_tags(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
    child = restore.DatasetPlan(f"{config.replica_dataset}/var", f"{config.target_dataset}/var", _SNAPSHOTS)
    commands = []
    monkeypatch.setattr(restore, "remote_zfs_run", lambda _config, *args: commands.append(args))

    def properties(_config, *args, **kwargs):
        target = args[-1]
        if target == root.target:
            return _completed("mountpoint\tnone\ncanmount\ton\nmounted\tno\n")
        return _completed("mountpoint\t/var\ncanmount\ton\nmounted\tno\n")

    monkeypatch.setattr(restore, "remote_zfs_capture", properties)
    monkeypatch.setattr(
        restore, "remote_zfs_value", lambda _config, *args: "true" if args[-1] == child.target else "false"
    )

    restore.finalize(config, [root, child])

    assert ("mount", root.target) not in commands
    assert ("mount", child.target) in commands
    assert ("set", "autobackup:bak=true", child.target) in commands
    assert ("set", "autobackup:bak=true", root.target) not in commands
