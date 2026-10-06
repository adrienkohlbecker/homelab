"""Unit tests for the ZFS restore safety checks the fixture round trip can't reach."""

import dataclasses
import shlex
import subprocess

import pytest
from conftest import load_repo_module

restore = load_repo_module("roles/zfs_autobackup/files/zfs_backup_restore.py")

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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["root@lab", "pool/src", _SNAPSHOTS[1], _SNAPSHOTS[0], "pool/dst", "/mnt"], "newer", id="inverted"
        ),
        pytest.param(["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "relative"], "normalized", id="relative"),
        pytest.param(["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/"], "normalized", id="root"),
        pytest.param(["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/../x"], "normalized", id="traversal"),
        pytest.param(["root@lab", "pool/src", *_SNAPSHOTS[:2], "pool/dst", "/mnt/x/"], "normalized", id="unnormalized"),
    ],
)
def test_parse_config_rejects_an_unusable_range_or_mountpoint(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        restore.parse_config(argv)

    assert error.value.code == 2
    assert expected in capsys.readouterr().err


def test_remote_commands_are_quoted_for_the_remote_shell(config, monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = "pool/dst; touch /tmp/pwned $(id)"
    config = dataclasses.replace(config, target_dataset=unsafe)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return _completed(returncode=1)

    monkeypatch.setattr(restore.subprocess, "run", run)

    restore.inspect_targets(config)

    ssh, remote_command = commands[0][:-1], commands[0][-1]
    assert ssh == ["ssh", "-n", config.target_ssh]
    # The remote shell must see the dataset as one word, metacharacters inert.
    assert shlex.split(remote_command)[-1] == unsafe


def test_resolve_sources_rejects_a_missing_endpoint(config, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command, **kwargs):
        if "snapshot" in command:
            raise restore.RestoreError("missing")
        return _completed(config.replica_dataset)

    monkeypatch.setattr(restore, "run", run)

    with pytest.raises(restore.RestoreError, match="range is incomplete"):
        restore.resolve_sources(config)


class TestTargetChecks:
    @pytest.mark.parametrize(
        ("failing", "returncode", "expected"),
        [
            pytest.param("mbuffer", 255, "cannot reach", id="unreachable"),
            pytest.param("mbuffer", 1, "mbuffer is missing", id="no_mbuffer"),
            pytest.param("--version", 1, "passwordless sudo zfs", id="no_passwordless_sudo"),
        ],
    )
    def test_preflight_rejects_a_target_missing_the_receive_leg(
        self, config, failing: str, returncode: int, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def remote(_config, *argv, **kwargs):
            return _completed(returncode=returncode, stderr="probe failed") if failing in argv else _completed()

        monkeypatch.setattr(restore, "remote", remote)

        with pytest.raises(restore.RestoreError, match=expected):
            restore.check_target_preflight(config)

    def test_absent_target_passes_inspection(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(restore, "remote_zfs", lambda *args, **kwargs: _completed(returncode=1))

        restore.inspect_targets(config)

    def test_unreachable_target_is_not_mistaken_for_absent(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            restore,
            "remote_zfs",
            lambda *args, **kwargs: _completed(returncode=255, stderr="ssh: connect: No route to host"),
        )

        with pytest.raises(restore.RestoreError, match="could not inspect"):
            restore.inspect_targets(config)

    def test_existing_tree_is_refused_with_the_recovery_commands(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        child = f"{config.target_dataset}/var"
        monkeypatch.setattr(
            restore, "remote_zfs", lambda *args, **kwargs: _completed(f"{config.target_dataset}\n{child}\n")
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


def test_single_snapshot_restore_sends_only_the_full_stream(config, monkeypatch: pytest.MonkeyPatch) -> None:
    config = dataclasses.replace(config, start_suffix=_SNAPSHOTS[-1])
    received = []
    monkeypatch.setattr(restore, "receive", lambda _config, command, target: received.append(command))

    restore.sync_dataset(config, config.replica_dataset)

    assert len(received) == 1
    assert "-I" not in received[0]


def _dataset(
    *,
    type_: str = "filesystem",
    mountpoint: str = "none",
    received: str = "-",
    canmount: str = "noauto",
    mounted: str = "no",
) -> dict[str, str]:
    """Build one dataset row as remote_properties would return it."""

    return {
        "type": type_,
        "mountpoint": mountpoint,
        "mountpoint_received": received,
        "canmount": canmount,
        "mounted": mounted,
        "autobackup:bak": "false",
    }


def _finalize(config, monkeypatch, planned, settled=None) -> list[tuple[str, ...]]:
    """Run finalize against canned property trees, returning the remote calls."""

    commands: list[tuple[str, ...]] = []
    reads = iter([planned, settled if settled is not None else planned])
    monkeypatch.setattr(restore, "remote_zfs", lambda _config, *args, **kwargs: commands.append(args))
    monkeypatch.setattr(restore, "remote_properties", lambda _config: next(reads))
    restore.finalize(config)
    return commands


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        # zfs send -b replays the source's absolute mountpoint; honouring
        # it would shadow the live directory of that name on the target.
        pytest.param("/mnt/services/sqlite", "{mountpoint}/sqlite", id="outside_the_restore_root"),
        pytest.param("-", "{mountpoint}/sqlite", id="absent_from_the_stream"),
        pytest.param("{mountpoint}/elsewhere", "{mountpoint}/elsewhere", id="contained"),
        pytest.param("none", "none", id="deliberately_unmounted"),
        pytest.param("legacy", "legacy", id="deliberately_legacy"),
    ],
)
def test_desired_mountpoint_contains_every_child(config, received: str, expected: str) -> None:
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
        assert [command[0] for command in commands[:5]] == ["set", "set", "inherit", "inherit", "set"]
        assert commands[:2] == [
            ("set", f"mountpoint={config.mountpoint}", config.target_dataset),
            ("set", f"mountpoint={config.mountpoint}/sqlite", child),
        ]

    def test_leaves_volumes_alone(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
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
            f"{config.target_dataset}/legacy": _dataset(mountpoint="legacy", canmount="on"),
            f"{config.target_dataset}/opt": _dataset(mountpoint=f"{config.mountpoint}/opt", canmount="off"),
            f"{config.target_dataset}/srv": _dataset(
                mountpoint=f"{config.mountpoint}/srv", canmount="on", mounted="yes"
            ),
        }
        commands = _finalize(config, monkeypatch, {config.target_dataset: _dataset()}, settled)

        assert [command for command in commands if command[0] == "mount"] == [("mount", child)]
