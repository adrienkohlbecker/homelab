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

        def capture(*args, **kwargs):
            if args[0] == "list":
                return _completed(f"{config.replica_dataset}\n{source_child}\n")
            return _completed("-")

        monkeypatch.setattr(restore, "zfs_capture", capture)
        monkeypatch.setattr(restore, "selected_snapshots", lambda _config, _dataset: _SNAPSHOTS)

        plans = restore.build_plans(config)

        assert [(plan.source, plan.target) for plan in plans] == [
            (config.replica_dataset, config.target_dataset),
            (source_child, f"{config.target_dataset}/var"),
        ]
        assert all(plan.snapshots == _SNAPSHOTS for plan in plans)

    def test_rejects_clone_datasets(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        def capture(*args, **kwargs):
            if args[0] == "list":
                return _completed(config.replica_dataset)
            return _completed("pool/origin@snapshot")

        monkeypatch.setattr(restore, "zfs_capture", capture)

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

    def test_preserves_the_zfs_diagnostic_for_an_unusable_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            restore,
            "zfs_capture",
            lambda *args, **kwargs: _completed("resume token is corrupt\n", returncode=1),
        )

        with pytest.raises(restore.RestoreError, match="receive resume token is unusable: resume token is corrupt"):
            restore.token_suffix("token_1")


class TestRemoteSnapshotRows:
    def test_fetches_names_and_guids_in_one_checked_call(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def capture(_config, *args, **kwargs):
            calls.append((args, kwargs))
            return _completed(
                f"{config.target_dataset}@{_SNAPSHOTS[0]}\tguid-1\n"
                f"{config.target_dataset}@{_SNAPSHOTS[1]}\tguid-2\n"
            )

        monkeypatch.setattr(restore, "remote_zfs_capture", capture)

        assert restore.remote_snapshot_rows(config, config.target_dataset) == [
            (_SNAPSHOTS[0], "guid-1"),
            (_SNAPSHOTS[1], "guid-2"),
        ]
        assert calls == [
            (
                (
                    "list",
                    "-H",
                    "-d",
                    "1",
                    "-t",
                    "snapshot",
                    "-o",
                    "name,guid",
                    "-p",
                    "-s",
                    "createtxg",
                    config.target_dataset,
                ),
                {},
            )
        ]

    def test_rejects_malformed_remote_output(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "remote_zfs_capture", lambda *args, **kwargs: _completed("missing-guid\n"))

        with pytest.raises(restore.RestoreError, match="invalid snapshot row"):
            restore.remote_snapshot_rows(config, config.target_dataset)


class TestInspectTargets:
    @staticmethod
    def _patch_state(
        monkeypatch: pytest.MonkeyPatch,
        *,
        datasets: list[str],
        rows: dict[str, list[tuple[str, str]]] | None = None,
        tokens: dict[str, str] | None = None,
        written: dict[str, str] | None = None,
        token_suffix: str | None = None,
    ) -> None:
        rows = rows or {}
        tokens = tokens or {}
        written = written or {}
        monkeypatch.setattr(restore, "remote_snapshot_rows", lambda _config, target: rows.get(target, []))

        def remote_capture(_config, *args, **kwargs):
            if args[0] == "list":
                return _completed("\n".join(datasets))
            property_name, target = args[-2:]
            if property_name == "receive_resume_token":
                return _completed(tokens.get(target, "-"))
            if property_name == "written":
                return _completed(written.get(target, "0"))
            raise AssertionError(f"unexpected remote property: {property_name}")

        monkeypatch.setattr(restore, "remote_zfs_capture", remote_capture)
        monkeypatch.setattr(restore, "snapshot_guid", lambda _dataset, suffix: f"guid-{suffix}")
        monkeypatch.setattr(restore, "run", lambda _command: None)
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

    def test_accepts_an_unchanged_completed_prefix(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(
            monkeypatch,
            datasets=[plan.target],
            rows={plan.target: [(_SNAPSHOTS[0], f"guid-{_SNAPSHOTS[0]}")]},
            written={plan.target: "0"},
        )

        restore.inspect_targets(config, [plan])

        assert plan.received_count == 1
        assert plan.resume_token is None

    def test_rejects_writes_after_a_completed_prefix(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(
            monkeypatch,
            datasets=[plan.target],
            rows={plan.target: [(_SNAPSHOTS[0], f"guid-{_SNAPSHOTS[0]}")]},
            written={plan.target: "4096"},
        )

        with pytest.raises(
            restore.RestoreError,
            match=rf"sudo zfs rollback {plan.target}@{_SNAPSHOTS[0]}",
        ):
            restore.inspect_targets(config, [plan])

    def test_rejects_snapshots_after_an_incomplete_prefix_with_recursive_rollback_guidance(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        self._patch_state(
            monkeypatch,
            datasets=[plan.target],
            rows={
                plan.target: [
                    (_SNAPSHOTS[0], f"guid-{_SNAPSHOTS[0]}"),
                    ("local-snapshot", "local-guid"),
                ]
            },
        )

        with pytest.raises(
            restore.RestoreError,
            match=rf"sudo zfs rollback -r {plan.target}@{_SNAPSHOTS[0]}",
        ):
            restore.inspect_targets(config, [plan])

    def test_safely_refuses_a_complete_range_with_later_snapshots(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        rows = [(suffix, f"guid-{suffix}") for suffix in _SNAPSHOTS]
        rows.append(("local-snapshot", "local-guid"))
        self._patch_state(monkeypatch, datasets=[plan.target], rows={plan.target: rows})

        with pytest.raises(restore.RestoreError, match="requested snapshot range and later snapshots") as error:
            restore.inspect_targets(config, [plan])

        assert "rollback" not in str(error.value)

    def test_rejects_writes_on_a_complete_dataset_while_its_tree_is_incomplete(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root_plan = restore.DatasetPlan(config.replica_dataset, config.target_dataset, _SNAPSHOTS)
        child_plan = restore.DatasetPlan(
            f"{config.replica_dataset}/child",
            f"{config.target_dataset}/child",
            _SNAPSHOTS,
        )
        root_rows = [(suffix, f"guid-{suffix}") for suffix in _SNAPSHOTS]
        self._patch_state(
            monkeypatch,
            datasets=[root_plan.target],
            rows={root_plan.target: root_rows},
            written={root_plan.target: "4096"},
        )

        with pytest.raises(
            restore.RestoreError,
            match=rf"sudo zfs rollback {root_plan.target}@{_SNAPSHOTS[-1]}",
        ):
            restore.inspect_targets(config, [root_plan, child_plan])

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
                "-su",
                config.target_dataset,
            ],
        ]
        assert "-n" not in commands[-1]

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

    def test_sync_resumes_then_sends_the_remaining_incremental(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = restore.DatasetPlan(
            config.replica_dataset,
            config.target_dataset,
            _SNAPSHOTS,
            received_count=1,
            resume_token="token_1",
        )
        received = []
        previews = []
        monkeypatch.setattr(restore, "run", previews.append)
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
        assert previews == [["sudo", "zfs", "send", "-nP", "-t", "token_1"]]


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
            return _completed("mountpoint\tnone\ncanmount\ton\nmounted\tno\nautobackup:bak\tfalse\n")
        return _completed("mountpoint\t/var\ncanmount\ton\nmounted\tno\nautobackup:bak\ttrue\n")

    monkeypatch.setattr(restore, "remote_zfs_capture", properties)

    restore.finalize(config, [root, child])

    assert ("mount", root.target) not in commands
    assert ("mount", child.target) in commands
    assert ("set", "autobackup:bak=true", child.target) in commands
    assert ("set", "autobackup:bak=true", root.target) not in commands
