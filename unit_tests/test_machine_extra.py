"""Unit tests for machine.py functions not covered by existing test_*.py files."""

import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import machine
import matrix
import pytest
import utils

# ---------------------------------------------------------------------------
# qemu_user_net_args
# ---------------------------------------------------------------------------


class TestQemuUserNetArgs:
    def test_returns_empty_for_unknown_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            machine, "_load_test_topology", lambda: {"hosts": {}, "partitions": {"physical": {"cidr": "10.234.0.0/16"}}}
        )
        assert machine.qemu_user_net_args("nonexistent") == ""

    def test_returns_string_for_known_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        topo = {
            "hosts": {"lab": {"physical": "10.234.0.2"}},
            "partitions": {"physical": {"cidr": "10.234.0.0/16"}},
        }
        monkeypatch.setattr(machine, "_load_test_topology", lambda: topo)
        result = machine.qemu_user_net_args("lab")
        assert result.startswith(",")
        assert "net=10.234.0.0/16" in result
        assert "dhcpstart=10.234.0.2" in result


# ---------------------------------------------------------------------------
# _workdir_is_orphan
# ---------------------------------------------------------------------------


class TestWorkdirIsOrphan:
    def test_orphan_when_no_live_file(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        assert machine._workdir_is_orphan(workdir) is True

    def test_orphan_when_live_unlocked(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        assert machine._workdir_is_orphan(workdir) is True

    def test_not_orphan_when_live_locked(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_test"
        workdir.mkdir()
        live = workdir / ".live"
        live.write_text("")
        fd = os.open(str(live), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            assert machine._workdir_is_orphan(workdir) is False
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# sweep_stale_workdirs
# ---------------------------------------------------------------------------


class TestSweepStaleWorkdirs:
    def test_reaps_old_unlocked_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_stale"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        old_time = time.time() - 120
        os.utime(str(workdir), (old_time, old_time))
        machine.sweep_stale_workdirs(tmp_path)
        assert not workdir.exists()

    def test_keeps_recent_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_recent"
        workdir.mkdir()
        (workdir / ".live").write_text("")
        machine.sweep_stale_workdirs(tmp_path)
        assert workdir.exists()

    def test_keeps_locked_tmpdir(self, tmp_path: Path) -> None:
        workdir = tmp_path / "tmp_locked"
        workdir.mkdir()
        live = workdir / ".live"
        live.write_text("")
        fd = os.open(str(live), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            old_time = time.time() - 120
            os.utime(str(workdir), (old_time, old_time))
            machine.sweep_stale_workdirs(tmp_path)
            assert workdir.exists()
        finally:
            os.close(fd)

    def test_ignores_nonexistent_dir(self) -> None:
        machine.sweep_stale_workdirs(Path("/nonexistent/path"))

    def test_ignores_non_tmp_dirs(self, tmp_path: Path) -> None:
        other = tmp_path / "regular_dir"
        other.mkdir()
        old_time = time.time() - 120
        os.utime(str(other), (old_time, old_time))
        machine.sweep_stale_workdirs(tmp_path)
        assert other.exists()

    def test_ignores_packer_build_dirs(self, tmp_path: Path) -> None:
        workdir = tmp_path / ".build-stale"
        workdir.mkdir()
        old_time = time.time() - 120
        os.utime(str(workdir), (old_time, old_time))

        machine.sweep_stale_workdirs(tmp_path)

        assert workdir.exists()


# ---------------------------------------------------------------------------
# QemuMachineSpec
# ---------------------------------------------------------------------------


class TestConstants:
    def test_qemu_specs_match_the_matrix_machines(self) -> None:
        assert set(machine.QEMU_MACHINE_SPECS) == set(matrix.MACHINES)


class TestLinkPackerArtifacts:
    @staticmethod
    def _publish(path: Path, version: str) -> None:
        path.mkdir()
        for name in ("packer-ubuntu-1.raw", "packer-ubuntu-2.raw", "efivars.fd"):
            (path / name).write_text(version)

    @pytest.fixture(autouse=True)
    def _no_tick_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_sleep() -> None:
            pass

        monkeypatch.setattr(machine, "sleep_tick", no_sleep)

    def test_links_the_published_artifacts(self, tmp_path: Path) -> None:
        published = tmp_path / "lab"
        self._publish(published, "v1")
        (published / "notes.txt").touch()
        dest = tmp_path / "base"

        machine.link_packer_artifacts(published, dest)

        assert sorted(p.name for p in dest.iterdir()) == ["efivars.fd", "packer-ubuntu-1.raw", "packer-ubuntu-2.raw"]
        assert (dest / "packer-ubuntu-1.raw").stat().st_ino == (published / "packer-ubuntu-1.raw").stat().st_ino

    @pytest.mark.parametrize("delete_old", [True, False])
    def test_retries_onto_the_version_swapped_in_mid_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_old: bool
    ) -> None:
        """A publish renames the old version away, swaps in the new one, and
        then deletes the old one; either state must yield only the new set."""
        published = tmp_path / "lab"
        self._publish(published, "v1")
        dest = tmp_path / "base"
        real_link = os.link
        swapped = False

        def link_with_swap(*args: Any, **kwargs: Any) -> None:
            nonlocal swapped
            if not swapped:
                swapped = True
                published.rename(tmp_path / ".lab.old")
                self._publish(published, "v2")
                if delete_old:
                    shutil.rmtree(tmp_path / ".lab.old")
            real_link(*args, **kwargs)

        monkeypatch.setattr(machine.os, "link", link_with_swap)

        machine.link_packer_artifacts(published, dest)

        assert {p.read_text() for p in dest.iterdir()} == {"v2"}
        assert len(list(dest.iterdir())) == 3

    def test_gives_up_when_nothing_is_published(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="kept changing"):
            machine.link_packer_artifacts(tmp_path / "lab", tmp_path / "base")


class TestDiscoverPackerDisks:
    def test_returns_contiguous_disks_and_format(self, tmp_path: Path) -> None:
        second = tmp_path / "packer-ubuntu-2.raw"
        first = tmp_path / "packer-ubuntu-1.raw"
        second.touch()
        first.touch()

        paths, disk_format = machine.discover_packer_disks(tmp_path)

        assert paths == [first, second]
        assert disk_format == "raw"

    def test_rejects_missing_disk_index(self, tmp_path: Path) -> None:
        (tmp_path / "packer-ubuntu-1.qcow2").touch()
        (tmp_path / "packer-ubuntu-3.qcow2").touch()

        with pytest.raises(RuntimeError, match=r"indexes.*\[1, 3\].*\[1, 2\]"):
            machine.discover_packer_disks(tmp_path)

    def test_rejects_mixed_formats(self, tmp_path: Path) -> None:
        (tmp_path / "packer-ubuntu-1.raw").touch()
        (tmp_path / "packer-ubuntu-2.qcow2").touch()

        with pytest.raises(RuntimeError, match="mix formats"):
            machine.discover_packer_disks(tmp_path)

    def test_rejects_empty_directory(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="no Packer disks"):
            machine.discover_packer_disks(tmp_path)


class TestUefiDrives:
    def test_creates_blank_vars_sized_to_code(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
    ) -> None:
        code = tmp_path / "code.fd"
        code.write_bytes(b"code")
        instance = machine_factory(host_arch="aarch64")
        instance.guest = {"uefi_firmware": {"darwin": {"code": str(code)}}}

        drives = instance._uefi_drives()

        blank_vars = instance.workdir_path / "uefi-vars.fd"
        assert blank_vars.read_bytes() == b"\0" * len(b"code")
        assert drives == [
            f"file={code},if=pflash,unit=0,format=raw,readonly=on",
            f"file={blank_vars},if=pflash,unit=1,format=raw",
        ]


class TestMachineArtifactOwnership:
    def test_clears_and_cleans_every_per_run_artifact(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        artifacts = [out / f"lab.noble.testrole.{suffix}.ansi" for suffix in ("output", "journal", "boot", "failure")]
        for artifact in artifacts:
            artifact.write_text("stale")

        instance = machine_factory()
        assert all(not artifact.exists() for artifact in artifacts)

        for artifact in artifacts:
            artifact.write_text("current")

        def passing() -> None:
            pass

        assert instance.run(passing, "lab.testrole") == 0
        assert all(not artifact.exists() for artifact in artifacts)

    @pytest.mark.parametrize(("label", "verdict"), [(None, "✓ passed"), ("site_test", "✓ site_test passed")])
    def test_the_passed_verdict_names_only_a_given_label(
        self,
        machine_factory: Callable[..., machine.Machine],
        capsys: pytest.CaptureFixture[str],
        label: str | None,
        verdict: str,
    ) -> None:
        instance = machine_factory()

        assert instance.run(lambda: None, label) == 0
        utils._drain_stdout()

        assert capsys.readouterr().out.splitlines()[-1] == utils.colorize(verdict, "green")

    @pytest.mark.parametrize(
        ("exc", "rc"),
        [
            (machine.CommandFailedException(["false"], 1, []), 1),
            (machine.CheckFailedException("settle"), 1),
            (machine.IdempotenceFailedException("changed"), 125),
            (TimeoutError(), 124),
            (ValueError("bug"), 1),
        ],
    )
    def test_run_maps_failures_to_exit_codes_and_keeps_logs(
        self, machine_factory: Callable[..., machine.Machine], exc: Exception, rc: int
    ) -> None:
        instance = machine_factory()
        instance.journal_file.write_text("evidence")

        def failing() -> None:
            raise exc

        assert instance.run(failing, "lab.testrole") == rc
        assert instance.journal_file.exists()
        assert instance.output_file.exists()
        assert instance.failure_file.exists()

    def test_run_shows_the_failed_task_and_ends_on_the_verdict(
        self,
        machine_factory: Callable[..., machine.Machine],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        instance = machine_factory()
        utils.use_compact_console("testrole", "lab:noble")
        stdout = [
            "TASK [testrole : Earlier] ******************************************************",
            "ok: [lab]",
            "TASK [testrole : Validate] *****************************************************",
            "fatal: [lab]: FAILED! => ",
            "    msg: bad config",
            "PLAY RECAP *********************************************************************",
        ]
        exc = machine.CommandFailedException(["ansible-playbook", "site.yml"], 2, ["[WARNING]: noise"], stdout)

        def failing() -> None:
            raise exc

        assert instance.run(failing, None) == 1
        utils._drain_stdout()

        lines = capsys.readouterr().out.splitlines()
        terminal = "\n".join(lines)
        assert "│ fatal: [lab]: FAILED! => " in terminal
        assert "│     msg: bad config" in terminal
        assert "Earlier" not in terminal
        assert "PLAY RECAP" not in terminal
        assert "[WARNING]: noise" not in terminal
        assert lines[-1].endswith("✗ failed at TASK [testrole : Validate]\x1b[0m")
        assert "[WARNING]: noise" in instance.output_file.read_text()
        assert instance.failure_file.read_text() == "\n".join(stdout[2:5]) + "\n"


class TestSystemReadiness:
    def test_accepts_running_state(
        self,
        machine_factory: Callable[..., machine.Machine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = machine_factory()

        def ssh_command(*args: str, check: bool = True) -> SimpleNamespace:
            assert args == (
                "timeout",
                str(machine.SYSTEM_RUNNING_WAIT_TIMEOUT),
                "systemctl",
                "is-system-running",
                "--wait",
            )
            assert check is False
            return SimpleNamespace(exitcode=0, stdout=["running"])

        monkeypatch.setattr(instance, "ssh_command", ssh_command)
        instance.ensure_system_running()

    def test_reports_failed_units(
        self,
        machine_factory: Callable[..., machine.Machine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = machine_factory()
        responses = iter(
            (
                SimpleNamespace(exitcode=1, stdout=["degraded"]),
                SimpleNamespace(exitcode=0, stdout=["broken.service loaded failed failed"]),
            )
        )

        def ssh_command(*args: str, check: bool = True) -> SimpleNamespace:
            assert check is False
            return next(responses)

        monkeypatch.setattr(instance, "ssh_command", ssh_command)
        with pytest.raises(RuntimeError, match=r"(?s)degraded.*broken\.service"):
            instance.ensure_system_running()


class TestAnsibleControllerStaging:
    def test_stages_once_on_first_ansible_command(
        self,
        machine_factory: Callable[..., machine.Machine],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for directory in (
            "group_vars",
            "host_vars",
            "roles",
            "data",
            "test/playbooks",
        ):
            (tmp_path / directory).mkdir(parents=True)
        (tmp_path / "test/playbooks/site.yml").write_text("fixture site\n")
        (tmp_path / "test/playbooks/_environment.yml").write_text("environment\n")

        staged_calls = 0

        def write_connection_inventory(self: machine.Machine) -> None:
            nonlocal staged_calls
            staged_calls += 1

        def run_command(*args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(exitcode=0, stdout=[])

        monkeypatch.setattr(machine.Machine, "_write_connection_inventory", write_connection_inventory)
        monkeypatch.setattr(machine, "run_command", run_command)

        m = machine_factory()
        assert not (m.workdir_path / "roles").exists()

        m.ansible_command(str(m.workdir_path / "site.yml"))
        m.ansible_command(str(m.workdir_path / "_environment.yml"))

        assert staged_calls == 1
        assert (m.workdir_path / "roles").is_dir()
        assert (m.workdir_path / "_environment.yml").read_text() == "environment\n"
        assert (m.workdir_path / "site.yml").read_text() == "fixture site\n"


def test_ensure_booted_reports_early_qemu_exit(
    machine_factory: Callable[..., machine.Machine],
) -> None:
    m = machine_factory(machine="lab", role="test")
    m.proc = cast(subprocess.Popen, SimpleNamespace(poll=lambda: 1))

    with pytest.raises(RuntimeError, match=r"qemu wrapper exited with 1.*lab\.noble\.test\.boot\.ansi"):
        m.ensure_booted()


# ---------------------------------------------------------------------------
# _read_host_ports: qemu binds each hostfwd to a free port; the harness reads
# the choices back over QMP.
# ---------------------------------------------------------------------------

# `info usernet` from QEMU 8.2 with one hostfwd per protocol beyond SSH.
INFO_USERNET = """\
Hub -1 (user.0):
  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP[HOST_FORWARD]  13       127.0.0.1 51234       10.0.2.15    22     0     0
  TCP[HOST_FORWARD]  14       127.0.0.1 51235       10.0.2.15 18080     0     0
  UDP[HOST_FORWARD]  15       127.0.0.1 51236       10.0.2.15 51820     0     0
  TCP[ESTABLISHED]   16       10.0.2.15 40000      10.0.2.2    22     0     0
"""


class TestReadHostPorts:
    def _machine(self, factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
        m = factory(**kwargs)
        calls: list[str] = []

        def fake_qmp(command: str, arguments: dict | None = None) -> object:
            calls.append(command)
            return {"service": "5901"} if command == "query-vnc" else INFO_USERNET

        monkeypatch.setattr(m, "_qmp", fake_qmp)
        monkeypatch.setattr(machine, "DEFAULT_WAN_FORWARDS", {"tcp": (18080,), "udp": (51820,)})
        return m, calls

    def test_maps_guest_ports_to_qemu_chosen_host_ports(
        self, machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m, calls = self._machine(machine_factory, monkeypatch)
        m._read_host_ports()
        assert m.ssh_port == 51234
        assert m.wan_forward_ports == {"tcp": {"18080": 51235}, "udp": {"51820": 51236}}
        assert calls == ["human-monitor-command"]

    def test_kept_headless_vm_reads_the_vnc_port(
        self, machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m, _ = self._machine(machine_factory, monkeypatch, keep_vm=True)
        m._read_host_ports()
        assert m.vnc_port == 5901

    def test_missing_forward_fails_loudly(
        self, machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m, _ = self._machine(machine_factory, monkeypatch)
        monkeypatch.setattr(machine, "DEFAULT_WAN_FORWARDS", {"tcp": (9092,), "udp": ()})
        with pytest.raises(KeyError):
            m._read_host_ports()


class TestQmpTransport:
    """_qmp against a fake monitor speaking the QMP wire protocol."""

    @staticmethod
    def _serve(
        path: Path, replies: list[dict], *, delay: float, events: int = 1, greeting_delay: float = 0
    ) -> threading.Thread:
        """Bind *path* after *delay* (as qemu opens its monitor after the
        pidfile) and answer one connection: greeting after *greeting_delay*
        (as qemu greets after machine init), capabilities, *events*
        asynchronous events 50ms apart, then *replies* to the command."""

        def run() -> None:
            time.sleep(delay)
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen(1)
                conn, _ = server.accept()
                # The client hangs up early when it gives up on the events.
                with contextlib.suppress(BrokenPipeError), conn, conn.makefile("rwb") as stream:

                    def send(message: dict) -> None:
                        stream.write(json.dumps(message).encode() + b"\n")
                        stream.flush()

                    time.sleep(greeting_delay)
                    send({"QMP": {"version": {}, "capabilities": []}})
                    stream.readline()
                    send({"return": {}})
                    stream.readline()
                    for _ in range(events):
                        send({"event": "NIC_RX_FILTER_CHANGED", "data": {}})
                        time.sleep(0.05)
                    for reply in replies:
                        send(reply)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    @pytest.fixture
    def m(self, machine_factory: Callable[..., machine.Machine]) -> machine.Machine:
        m = machine_factory()
        # A unix socket path must stay under ~104 bytes; pytest's tmp_path can exceed it.
        m.qmp_socket = Path(tempfile.mkdtemp(dir="/tmp")) / "qmp"
        return m

    def test_waits_for_a_late_monitor_and_skips_events(self, m: machine.Machine) -> None:
        server = self._serve(m.qmp_socket, [{"return": {"service": "5901"}}], delay=0.5)
        assert m._qmp("query-vnc") == {"service": "5901"}
        server.join(5)

    def test_a_qmp_error_raises(self, m: machine.Machine) -> None:
        self._serve(m.qmp_socket, [{"error": {"class": "GenericError", "desc": "no VNC"}}], delay=0)
        with pytest.raises(RuntimeError, match="no VNC"):
            m._qmp("query-vnc")

    def test_a_stream_of_events_cannot_hold_the_reply_open(
        self, m: machine.Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(machine, "QMP_TIMEOUT", 0.3)
        self._serve(m.qmp_socket, [], delay=0, events=100)
        with pytest.raises(TimeoutError, match="no reply"):
            m._qmp("query-vnc")

    def test_a_silent_monitor_names_the_command_it_timed_out_on(
        self, m: machine.Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(machine, "QMP_TIMEOUT", 0.3)
        self._serve(m.qmp_socket, [], delay=0, greeting_delay=1)
        with pytest.raises(TimeoutError, match="QMP qmp_capabilities got no reply"):
            m._qmp("query-vnc")

    def test_a_monitor_that_never_opens_times_out(self, m: machine.Machine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine, "QMP_TIMEOUT", 0.3)
        with pytest.raises(FileNotFoundError):
            m._qmp("query-vnc")


class TestWait:
    @pytest.fixture
    def m(self, machine_factory: Callable[..., machine.Machine]) -> Iterator[machine.Machine]:
        m = machine_factory()
        m.proc = subprocess.Popen(["sleep", "30"])
        yield m
        m.proc.kill()
        m.proc.wait()

    def test_the_session_deadline_caps_the_wait(self, m: machine.Machine) -> None:
        m.deadline = time.monotonic() + 0.3
        with pytest.raises(TimeoutError):
            m.wait(120)

    def test_a_shorter_timeout_still_raises_timeout_expired(self, m: machine.Machine) -> None:
        m.deadline = time.monotonic() + 120
        with pytest.raises(subprocess.TimeoutExpired):
            m.wait(0.3)


def test_stop_kills_a_process_group_that_ignores_sigterm(
    machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group holds the timeout wrapper and qemu, so killing it reaches qemu
    even before its pidfile exists."""
    monkeypatch.setattr(machine, "STOP_GRACE_SECONDS", 0.3)
    m = machine_factory()
    m.proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60 & wait"], start_new_session=True)
    time.sleep(0.2)

    m.stop()

    assert m.proc.returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.killpg(m.proc.pid, 0)
    assert not m.workdir_path.exists()


@pytest.mark.parametrize(
    "group",
    [
        # The timeout wrapper died first; qemu lives on in its group.
        "sleep 60 &",
        # The wrapper exits on SIGTERM; a member ignoring it needs SIGKILL.
        "(trap '' TERM; sleep 60) & wait",
    ],
)
def test_stop_kills_members_that_outlive_the_group_leader(
    machine_factory: Callable[..., machine.Machine], monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    monkeypatch.setattr(machine, "STOP_GRACE_SECONDS", 0.3)
    m = machine_factory()
    m.proc = subprocess.Popen(["sh", "-c", group], start_new_session=True)
    time.sleep(0.2)

    m.stop()

    with pytest.raises((ProcessLookupError, PermissionError)):
        os.killpg(m.proc.pid, 0)


def test_sigterm_stops_a_cell_like_ctrl_c(machine_factory: Callable[..., machine.Machine]) -> None:
    """GNU parallel's --termseq and CI cancels send SIGTERM."""
    m = machine_factory()

    def terminated() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert m.run(terminated, "lab.testrole") == 130
    finally:
        signal.signal(signal.SIGTERM, previous)
