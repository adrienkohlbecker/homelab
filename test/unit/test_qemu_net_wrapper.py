"""Tests for the packer qemu_binary shim's arg parsing.

The passt datapath itself only runs in the noble ci-image (Linux + qemu 8.2 +
passt) so it can't be exercised here, but the fiddly part -- pulling the netdev
id + host-forwards out of packer's generated `-netdev user,...` and rebuilding
them as passt port specs -- is pure string work and gets pinned here.
"""

import importlib.util
from pathlib import Path

_WRAPPER = Path(__file__).parents[2] / "packer" / "qemu_net_wrapper.py"
_spec = importlib.util.spec_from_file_location("qemu_net_wrapper", _WRAPPER)
assert _spec and _spec.loader
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


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


def test_parse_netdev_user_extracts_id_and_forward() -> None:
    netid, fwds = wrapper._parse_netdev_user("user,id=user.0,hostfwd=tcp::2222-:22")
    assert netid == "user.0"
    assert fwds == [("tcp", "2222", "22")]


def test_parse_netdev_user_handles_explicit_host_addr_and_multiple_forwards() -> None:
    netid, fwds = wrapper._parse_netdev_user(
        "user,id=net0,hostfwd=tcp:127.0.0.1:2230-:22,hostfwd=udp::5300-:53"
    )
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
