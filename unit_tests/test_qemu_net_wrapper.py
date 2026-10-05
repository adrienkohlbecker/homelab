"""Tests for the packer qemu_binary shim's arg parsing.

The passt datapath itself only runs in the noble ci-image (Linux + qemu 8.2 +
passt) so it can't be exercised here, but the fiddly part -- pulling the netdev
id + host-forwards out of packer's generated `-netdev user,...` and rebuilding
them as passt port specs -- is pure string work and gets pinned here.
"""

import subprocess

import pytest

import packer.qemu_net_wrapper as wrapper

# A representative argv slice as packer's qemu plugin emits it: the user-netdev
# with the SSH host-forward, plus the device that references it by id.
PACKER_ARGS = [
    "-m",
    "4096",
    "-netdev",
    "user,id=user.0,hostfwd=tcp::2222-:22",
    "-device",
    "virtio-net,netdev=user.0",
]


def test_find_user_netdev_locates_the_user_netdev() -> None:
    assert wrapper._find_user_netdev(PACKER_ARGS) == 2


def test_find_user_netdev_none_on_a_version_probe() -> None:
    # `qemu_binary -version` has no -netdev to rewrite -> pass through.
    assert wrapper._find_user_netdev(["-version"]) is None


def test_real_qemu_derives_the_emulator_from_the_host_arch(monkeypatch) -> None:
    monkeypatch.setattr(wrapper.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(wrapper.platform, "machine", lambda: "x86_64")

    assert wrapper._real_qemu() == "/usr/bin/qemu-system-x86_64"


def test_real_qemu_normalizes_mac_arm64(monkeypatch) -> None:
    monkeypatch.setattr(wrapper.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(wrapper.platform, "machine", lambda: "arm64")

    assert wrapper._real_qemu() == "/usr/bin/qemu-system-aarch64"


def test_real_qemu_exits_when_the_emulator_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(wrapper.shutil, "which", lambda binary: None)
    monkeypatch.setattr(wrapper.platform, "machine", lambda: "x86_64")

    with pytest.raises(SystemExit, match="not found on PATH"):
        wrapper._real_qemu()


def test_machine_type_takes_the_type_from_packers_machine_arg() -> None:
    assert wrapper._machine_type(["-machine", "type=virt,accel=kvm", *PACKER_ARGS]) == "virt"


def test_machine_type_none_without_a_machine_arg() -> None:
    assert wrapper._machine_type(PACKER_ARGS) is None


def _fake_aarch64_qemu(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Mimic qemu-system-aarch64, which has no default machine: `-netdev help`
    only lists the netdev types once a machine is named."""
    if "-machine" not in argv:
        return subprocess.CompletedProcess(
            argv, 1, "", "qemu-system-aarch64: No machine specified, and there is no default\n"
        )
    return subprocess.CompletedProcess(argv, 0, "Available netdev backend types:\nsocket\nstream\n", "")


def test_passt_usable_on_aarch64_probes_with_packers_machine(monkeypatch) -> None:
    monkeypatch.setattr(wrapper.platform, "system", lambda: "Linux")
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wrapper.subprocess, "run", _fake_aarch64_qemu)
    args = ["-machine", "type=virt,accel=kvm", *PACKER_ARGS]

    assert wrapper._passt_usable("/usr/bin/qemu-system-aarch64", wrapper._machine_type(args)) is True


def test_parse_netdev_user_extracts_id_and_forward() -> None:
    netid, fwds = wrapper._parse_netdev_user("user,id=user.0,hostfwd=tcp::2222-:22")
    assert netid == "user.0"
    assert fwds == [("tcp", "2222", "22")]


def test_parse_netdev_user_handles_explicit_host_addr_and_multiple_forwards() -> None:
    netid, fwds = wrapper._parse_netdev_user("user,id=net0,hostfwd=tcp:127.0.0.1:2230-:22,hostfwd=udp::5300-:53")
    assert netid == "net0"
    assert fwds == [("tcp", "2230", "22"), ("udp", "5300", "53")]


def test_passt_port_args_single_addr_prefix_for_the_tcp_list() -> None:
    # The addr/ prefix binds the whole comma-list and must appear once (passt
    # rejects addr/a,addr/b). One forward -> one tcp spec, no udp flag.
    assert wrapper._passt_port_args([("tcp", "2222", "22")]) == [
        "--tcp-ports",
        "127.0.0.1/2222:22",
    ]


def test_passt_port_args_groups_tcp_and_udp() -> None:
    out = wrapper._passt_port_args([("tcp", "2222", "22"), ("udp", "5300", "53")])
    assert out == [
        "--tcp-ports",
        "127.0.0.1/2222:22",
        "--udp-ports",
        "127.0.0.1/5300:53",
    ]


def test_passt_command_assigns_an_isolated_guest_address() -> None:
    out = wrapper._passt_command("/tmp/passt.sock", [("tcp", "2222", "22")], None, False)

    # Only the addressing triple -- port rendering has its own test, and a
    # whole-argv equality would fail on any unrelated flag.
    start = out.index("--address")
    assert out[start : start + 6] == [
        "--address",
        "192.0.2.2",
        "--netmask",
        "255.255.255.0",
        "--gateway",
        "192.0.2.1",
    ]


def test_passt_command_keeps_the_guest_off_host_v6() -> None:
    # Without this the guest picks up a host-derived v6 address and route.
    assert "--ipv4-only" in wrapper._passt_command("/tmp/passt.sock", [], None, False)


def test_passt_command_isolates_the_host_only_on_request() -> None:
    # The gateway mapping is what lets passt relay DNS to a loopback stub
    # resolver, so only hosts that opt in (lab) drop it.
    assert "--no-map-gw" not in wrapper._passt_command("/tmp/passt.sock", [], None, False)
    assert "--no-map-gw" in wrapper._passt_command("/tmp/passt.sock", [], None, True)


def test_passt_command_advertises_only_a_configured_resolver() -> None:
    assert "--dns" not in wrapper._passt_command("/tmp/passt.sock", [], None, False)

    out = wrapper._passt_command("/tmp/passt.sock", [], "10.123.1.224", False)
    assert out[out.index("--dns") + 1] == "10.123.1.224"


class _Exec(Exception):
    """Raised by the fake os.execv so main() stops where it would exec qemu."""


def _run_main(monkeypatch, backend: str, *, usable: bool) -> tuple[list[str], list[list[str]]]:
    """Run main() on packer's argv; return the argv it would exec qemu with
    and the passt commands it would launch."""
    started: list[list[str]] = []
    monkeypatch.setenv("HOMELAB_NET_BACKEND", backend)
    monkeypatch.delenv("QEMU_NET_WRAPPER_LOG", raising=False)
    monkeypatch.setattr(wrapper, "_real_qemu", lambda: "/usr/bin/qemu-system-x86_64")
    monkeypatch.setattr(wrapper, "_passt_usable", lambda _q, _m: usable)
    monkeypatch.setattr(wrapper, "_start_passt", lambda _s, cmd: started.append(cmd))
    monkeypatch.setattr(wrapper.sys, "argv", ["qemu_net_wrapper.py", *PACKER_ARGS])

    def fake_execv(_path: str, argv: list[str]) -> None:
        raise _Exec(argv)

    monkeypatch.setattr(wrapper.os, "execv", fake_execv)
    with pytest.raises(_Exec) as exc:
        wrapper.main()
    return exc.value.args[0], started


def test_main_slirp_override_skips_the_probe(monkeypatch) -> None:
    argv, started = _run_main(monkeypatch, "slirp", usable=True)
    assert "user,id=user.0,hostfwd=tcp::2222-:22" in argv
    assert started == []


def test_main_auto_rewrites_the_netdev_when_passt_is_usable(monkeypatch) -> None:
    argv, _started = _run_main(monkeypatch, "auto", usable=True)
    assert any(arg.startswith("stream,id=user.0,") for arg in argv)


def test_main_passt_override_fails_when_passt_is_unusable(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_NET_BACKEND", "passt")
    monkeypatch.setattr(wrapper, "_real_qemu", lambda: "/usr/bin/qemu-system-x86_64")
    monkeypatch.setattr(wrapper, "_passt_usable", lambda _q, _m: False)
    monkeypatch.setattr(wrapper.sys, "argv", ["qemu_net_wrapper.py", *PACKER_ARGS])
    with pytest.raises(SystemExit, match="passt is unusable"):
        wrapper.main()


def test_main_wires_the_lab_environment_into_passt(monkeypatch) -> None:
    monkeypatch.setenv("QEMU_NET_WRAPPER_DNS", "10.123.1.224")
    monkeypatch.setenv("QEMU_NET_WRAPPER_ISOLATE_HOST", "1")
    _argv, [cmd] = _run_main(monkeypatch, "auto", usable=True)

    assert "--no-map-gw" in cmd
    assert cmd[cmd.index("--dns") + 1] == "10.123.1.224"


def test_main_rejects_a_malformed_isolate_flag(monkeypatch) -> None:
    monkeypatch.setenv("QEMU_NET_WRAPPER_ISOLATE_HOST", "yes")
    with pytest.raises(SystemExit, match="not in 0/1"):
        _run_main(monkeypatch, "auto", usable=True)
