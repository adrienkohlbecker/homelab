"""Unit tests for the ZFS restore planner and safety checks."""

import subprocess

import pytest
from conftest import load_role_module

restore = load_role_module("roles/zfs_autobackup/files/zfs_backup_restore.py")

_SNAPSHOTS = ["bak-20260801000000", "bak-20260802000000", "bak-20260803000000"]


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def config():
    return restore.Config(
        "root@lab",
        "apoc/lab/rpool/ROOT/noble",
        _SNAPSHOTS[0],
        _SNAPSHOTS[-1],
        "rpool/ROOT/noble",
        "/mnt/zfs_restore_validation/noble",
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
        ("argv", "expected"),
        [
            pytest.param(["root@lab"], "the following arguments are required", id="missing_arguments"),
            # argparse treats a leading-dash token as an option, so the six
            # positionals shift left and target_ssh fails on the next value.
            # The token is rejected either way -- it never reaches the ssh argv.
            pytest.param(
                ["-oProxyCommand=bad", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt"],
                "unsupported SSH destination",
                id="ssh_option_as_destination",
            ),
            pytest.param(
                ["root@lab", "pool/src;bad", *_SNAPSHOTS[:2], "pool/dst", "/mnt"],
                "unsupported replica dataset name",
                id="shell_metacharacter_in_dataset",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst;bad", "/mnt"],
                "unsupported target dataset name",
                id="shell_metacharacter_in_target",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "relative"],
                "unsupported target mountpoint",
                id="relative_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/"],
                "unsupported target mountpoint",
                id="root_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt//bad"],
                "unsupported target mountpoint",
                id="double_slash_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/bad/"],
                "unsupported target mountpoint",
                id="trailing_slash_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/./bad"],
                "unsupported target mountpoint",
                id="dot_component_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/../bad"],
                "unsupported target mountpoint",
                id="traversal_mountpoint",
            ),
            pytest.param(
                ["root@lab", "pool/src", "manual", _SNAPSHOTS[1], "pool/dst", "/mnt"],
                "unsupported snapshot suffix",
                id="non_bak_suffix",
            ),
            pytest.param(
                ["root@lab", "pool/src", _SNAPSHOTS[1], _SNAPSHOTS[0], "pool/dst", "/mnt"],
                "starting snapshot is newer",
                id="inverted_range",
            ),
        ],
    )
    def test_rejects_invalid_or_shell_unsafe_arguments(
        self, argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as error:
            restore.parse_config(argv)

        assert error.value.code == 2
        assert expected in capsys.readouterr().err


class TestResolveSources:
    def test_lists_the_replica_tree_without_snapshots(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        source_child = f"{config.replica_dataset}/var"
        calls = []

        def capture(*args, **kwargs):
            calls.append(args)
            assert args[0] == "list"
            if "snapshot" not in args:
                return _completed(f"{config.replica_dataset}\n{source_child}\n")
            return _completed()

        monkeypatch.setattr(restore, "zfs_capture", capture)

        sources = restore.resolve_sources(config)

        assert sources == [config.replica_dataset, source_child]
        # Without an explicit -t, a pool with listsnapshots=on would return
        # snapshot rows that the dataset-name check then rejects.
        assert calls[0] == (
            "list",
            "-r",
            "-H",
            "-t",
            "filesystem,volume",
            "-o",
            "name",
            config.replica_dataset,
        )
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

    def test_maps_sources_onto_the_target_tree(self, config) -> None:
        assert restore.target_dataset_for(config, config.replica_dataset) == config.target_dataset
        assert restore.target_dataset_for(config, f"{config.replica_dataset}/var") == f"{config.target_dataset}/var"

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
            restore.resolve_sources(config)


class TestTargetPreflight:
    def test_accepts_a_reachable_target_with_mbuffer_and_sudo(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "capture", lambda *args, **kwargs: _completed())

        restore.check_target_preflight(config)

    @pytest.mark.parametrize(
        ("failing", "expected"),
        [
            pytest.param("true", "cannot reach", id="unreachable"),
            pytest.param("mbuffer", "mbuffer is missing", id="no_mbuffer"),
            pytest.param("--version", "passwordless sudo zfs", id="no_passwordless_sudo"),
        ],
    )
    def test_rejects_a_target_missing_the_receive_leg(
        self, config, failing: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def capture(command, **kwargs):
            return _completed(returncode=1, stderr="probe failed") if failing in command else _completed()

        monkeypatch.setattr(restore, "capture", capture)

        with pytest.raises(restore.RestoreError, match=expected):
            restore.check_target_preflight(config)


class TestInspectTargets:
    def test_accepts_an_absent_target_tree(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        # zfs list exits 1 when the dataset does not exist.
        monkeypatch.setattr(
            restore,
            "remote_zfs_capture",
            lambda *args, **kwargs: _completed(returncode=1, stderr="cannot open: dataset does not exist"),
        )

        restore.inspect_targets(config)

    def test_scopes_the_probe_to_the_target_tree(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def capture(_config, *args, **kwargs):
            calls.append((args, kwargs))
            return _completed(returncode=1)

        monkeypatch.setattr(restore, "remote_zfs_capture", capture)

        restore.inspect_targets(config)

        assert calls == [
            (
                ("list", "-r", "-H", "-t", "filesystem,volume", "-o", "name", config.target_dataset),
                {"check": False},
            )
        ]

    def test_reports_an_unreachable_target_rather_than_calling_it_absent(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            restore,
            "remote_zfs_capture",
            lambda *args, **kwargs: _completed(returncode=255, stderr="ssh: connect: No route to host"),
        )

        with pytest.raises(restore.RestoreError, match="could not inspect"):
            restore.inspect_targets(config)

    def test_rejects_an_existing_tree_and_names_the_recovery_commands(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        child = f"{config.target_dataset}/var"
        monkeypatch.setattr(
            restore,
            "remote_zfs_capture",
            lambda *args, **kwargs: _completed(f"{config.target_dataset}\n{child}\n"),
        )

        with pytest.raises(restore.RestoreError) as error:
            restore.inspect_targets(config)

        message = str(error.value)
        assert "already exists" in message
        # A partially received dataset must have its resume token aborted
        # before the tree can be destroyed.
        assert f"sudo zfs receive -A {config.target_dataset}" in message
        assert f"sudo zfs receive -A {child}" in message
        assert f"sudo zfs destroy -r {config.target_dataset}" in message


class TestStreaming:
    def test_receive_builds_a_buffered_uncompressed_nonmultiplexed_pipeline(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = []
        monkeypatch.setattr(restore, "run", commands.append)

        restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

        assert commands == [
            [
                "bash",
                "-o",
                "pipefail",
                "-c",
                "zfs send pool/src@snapshot"
                " | mbuffer -m 256M"
                " | ssh -o ControlPath=none -o Compression=no -o 'Ciphers=^aes128-gcm@openssh.com'"
                " -o ServerAliveInterval=30 -o ServerAliveCountMax=6 root@lab"
                " 'mbuffer -q -m 256M | sudo -n zfs recv -s -u"
                " -o readonly=on -o canmount=noauto -o mountpoint=none rpool/ROOT/noble'",
            ]
        ]

    def test_receive_echoes_the_pipeline_before_running_it(
        self, config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(restore, "run", lambda command: None)

        restore.receive(config, ["zfs", "send", "pool/src@snapshot"], config.target_dataset)

        # Rerunning one dataset by hand is the documented mid-restore fallback.
        assert capsys.readouterr().out.startswith("$ zfs send pool/src@snapshot | mbuffer")

    def test_sync_sends_one_full_and_one_aggregate_incremental(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        received = []
        monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append((command, target)))

        restore.sync_dataset(config, config.replica_dataset)

        assert received == [
            (
                ["sudo", "zfs", "send", "-bpcveL", f"{config.replica_dataset}@{_SNAPSHOTS[0]}"],
                config.target_dataset,
            ),
            (
                [
                    "sudo",
                    "zfs",
                    "send",
                    "-bpcveL",
                    "-I",
                    f"@{_SNAPSHOTS[0]}",
                    f"{config.replica_dataset}@{_SNAPSHOTS[2]}",
                ],
                config.target_dataset,
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
        received = []
        monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append((command, target)))

        restore.sync_dataset(config, config.replica_dataset)

        assert received == [
            (
                ["sudo", "zfs", "send", "-bpcveL", f"{config.replica_dataset}@{_SNAPSHOTS[-1]}"],
                config.target_dataset,
            )
        ]


def _dataset(
    *,
    type_: str = "filesystem",
    mountpoint: str = "none",
    received: str = "-",
    canmount: str = "noauto",
    mounted: str = "no",
    tagged: str = "false",
) -> dict[str, str]:
    """Build one dataset row as remote_properties would return it."""

    return {
        "type": type_,
        "mountpoint": mountpoint,
        "mountpoint_received": received,
        "canmount": canmount,
        "mounted": mounted,
        "autobackup:bak": tagged,
    }


def _finalize(config, monkeypatch, planned, settled=None) -> list[tuple[str, ...]]:
    """Run finalize against canned property trees, returning the remote calls."""

    commands: list[tuple[str, ...]] = []
    reads = iter([planned, settled if settled is not None else planned])
    monkeypatch.setattr(restore, "remote_zfs_run", lambda _config, *args: commands.append(args))
    monkeypatch.setattr(restore, "remote_properties", lambda _config: next(reads))
    restore.finalize(config)
    return commands


class TestDesiredMountpoint:
    def test_pins_the_root_to_the_requested_mountpoint(self, config) -> None:
        assert restore.desired_mountpoint(config, config.target_dataset, _dataset(received="/mnt/services")) == (
            config.mountpoint
        )

    @pytest.mark.parametrize(
        ("received", "expected"),
        [
            # zfs send -b replays the source's absolute mountpoint; honouring
            # it would shadow the live directory of that name on the target.
            pytest.param("/mnt/services/sqlite", "{mountpoint}/sqlite", id="outside_the_restore_root"),
            pytest.param("-", "{mountpoint}/sqlite", id="absent_from_the_stream"),
            pytest.param("{mountpoint}/sqlite", "{mountpoint}/sqlite", id="already_contained"),
            pytest.param("{mountpoint}/elsewhere", "{mountpoint}/elsewhere", id="contained_but_not_derived"),
            pytest.param("none", "none", id="deliberately_unmounted"),
            pytest.param("legacy", "legacy", id="deliberately_legacy"),
        ],
    )
    def test_contains_every_child_under_the_restore_root(self, config, received: str, expected: str) -> None:
        child = f"{config.target_dataset}/sqlite"
        values = _dataset(received=received.format(mountpoint=config.mountpoint))

        assert restore.desired_mountpoint(config, child, values) == expected.format(mountpoint=config.mountpoint)


class TestFinalize:
    def test_anchors_every_mountpoint_before_releasing_the_pins(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        child = f"{config.target_dataset}/sqlite"
        commands = _finalize(
            config,
            monkeypatch,
            {
                config.target_dataset: _dataset(received="/mnt/services"),
                child: _dataset(received="/mnt/services/sqlite"),
            },
        )

        # Reverting a mountpoint from none to a real path can remount the
        # dataset there and then, so no source-absolute path may go live.
        assert commands[:5] == [
            ("set", f"mountpoint={config.mountpoint}", config.target_dataset),
            ("set", f"mountpoint={config.mountpoint}/sqlite", child),
            ("inherit", "-S", "-r", "readonly", config.target_dataset),
            ("inherit", "-S", "-r", "canmount", config.target_dataset),
            ("set", "readonly=off", "canmount=on", config.target_dataset),
        ]

    def test_leaves_volumes_unmounted(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        volume = f"{config.target_dataset}/vm"
        commands = _finalize(
            config,
            monkeypatch,
            {
                config.target_dataset: _dataset(),
                volume: _dataset(type_="volume", mountpoint="-"),
            },
        )

        assert not [command for command in commands if volume in command]

    def test_mounts_only_eligible_datasets(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        child = f"{config.target_dataset}/var"
        settled = {
            config.target_dataset: _dataset(mountpoint="none", canmount="on"),
            child: _dataset(mountpoint=f"{config.mountpoint}/var", canmount="on"),
            f"{config.target_dataset}/opt": _dataset(mountpoint=f"{config.mountpoint}/opt", canmount="off"),
            f"{config.target_dataset}/srv": _dataset(
                mountpoint=f"{config.mountpoint}/srv", canmount="on", mounted="yes"
            ),
        }
        commands = _finalize(config, monkeypatch, {config.target_dataset: _dataset()}, settled)

        assert [command for command in commands if command[0] == "mount"] == [("mount", child)]

    def test_relocalizes_received_tags_in_one_call(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        child = f"{config.target_dataset}/var"
        settled = {
            config.target_dataset: _dataset(tagged="true"),
            child: _dataset(tagged="true"),
            f"{config.target_dataset}/opt": _dataset(tagged="false"),
        }
        commands = _finalize(config, monkeypatch, {config.target_dataset: _dataset()}, settled)

        # Received-source tags are invisible to the local snapshot picker.
        assert commands[-1] == ("set", "autobackup:bak=true", config.target_dataset, child)


def test_remote_properties_groups_a_recursive_read_by_dataset(config, monkeypatch: pytest.MonkeyPatch) -> None:
    child = f"{config.target_dataset}/var"
    calls = []

    def capture(_config, *args, **kwargs):
        calls.append(args)
        return _completed(
            f"{config.target_dataset}\tmountpoint\tnone\t{config.mountpoint}\n"
            f"{config.target_dataset}\tcanmount\tnoauto\ton\n"
            f"{child}\tmountpoint\tnone\t-\n"
            f"{child}\tcanmount\tnoauto\toff\n"
        )

    monkeypatch.setattr(restore, "remote_zfs_capture", capture)

    assert restore.remote_properties(config) == {
        config.target_dataset: {
            "mountpoint": "none",
            "mountpoint_received": config.mountpoint,
            "canmount": "noauto",
            "canmount_received": "on",
        },
        child: {
            "mountpoint": "none",
            "mountpoint_received": "-",
            "canmount": "noauto",
            "canmount_received": "off",
        },
    }
    # Without -t, zfs get recurses into snapshots, which carry none of these.
    assert calls[0][:6] == ("get", "-r", "-H", "-t", "filesystem,volume", "-o")
