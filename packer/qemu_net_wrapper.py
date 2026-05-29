#!/usr/bin/env python3
"""qemu_binary shim that backs packer's build-VM NIC with passt when available.

packer's qemu plugin generates `-netdev user,id=<id>,hostfwd=tcp::<port>-:22`
plus a paired `-device <model>,netdev=<id>` for the build VM. libslirp's
single-threaded userspace stack drops UDP under host contention (several
variants build in parallel, nested in the runner's slirp4netns), which flakes
the build VM's DNS -- the same failure the test harness fixed by switching the
guest NIC to passt (test/machine.py).

This shim does the same for packer. When passt and qemu's `-netdev stream`
transport are both present (the noble ci-image, qemu 8.2 + passt), it starts a
passt sidecar and rewrites the `-netdev user,...` argument to point at passt's
unix socket, preserving the SSH host-forward so packer's communicator still
reaches the guest at 127.0.0.1:<port>. On a host without passt (a dev-Mac
`mise run packer:build`, or any qemu < 7.2), it execs qemu unchanged, so the
slirp path stays byte-for-byte identical.

Wired in via `qemu_binary` in packer/qemu.pkr.hcl. The arch's real emulator is
resolved from PATH here (arch is 1:1 with the host, same assumption the HCL
arch_table makes), so one shim serves both x86_64 and aarch64.

Set PACKER_NET_BACKEND={auto,slirp,passt} to override the probe (mirrors the
harness's HOMELAB_NET_BACKEND): `slirp` forces passthrough, `passt` fails loudly
if passt is unusable, `auto` (default) follows the capability probe.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time


def _real_qemu() -> str:
    arch = platform.machine()
    arch = "aarch64" if arch == "arm64" else arch
    binary = shutil.which(f"qemu-system-{arch}")
    if binary is None:
        sys.exit(f"qemu-net-wrapper: qemu-system-{arch} not found on PATH")
    return binary


def _passt_usable(real_qemu: str) -> bool:
    """True when passt is on PATH and this qemu advertises `-netdev stream`."""
    if platform.system() != "Linux":
        return False
    if shutil.which("passt") is None:
        return False
    try:
        probe = subprocess.run(
            [real_qemu, "-netdev", "help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "stream" in (probe.stdout + probe.stderr)


def _find_user_netdev(args: list[str]) -> int | None:
    """Index of the `-netdev` flag whose value is a `user,...` netdev, or None."""
    for i, arg in enumerate(args[:-1]):
        if arg == "-netdev" and args[i + 1].startswith("user"):
            return i
    return None


def _parse_netdev_user(value: str) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Pull the netdev id and (proto, host_port, guest_port) host-forwards out
    of a `user,id=...,hostfwd=...` netdev value."""
    netid = None
    fwds: list[tuple[str, str, str]] = []
    for part in value.split(","):
        if part.startswith("id="):
            netid = part[len("id=") :]
        elif part.startswith("hostfwd="):
            # hostfwd=<proto>:[hostaddr]:hostport-[guestaddr]:guestport
            proto, rest = part[len("hostfwd=") :].split(":", 1)
            host_side, guest_side = rest.split("-", 1)
            _hostaddr, host_port = host_side.rsplit(":", 1)
            _guestaddr, guest_port = guest_side.rsplit(":", 1)
            fwds.append((proto, host_port, guest_port))
    return netid, fwds


def _passt_port_args(fwds: list[tuple[str, str, str]]) -> list[str]:
    """Build passt --tcp-ports/--udp-ports from packer's host-forward set.

    All forwards bind 127.0.0.1 (where packer's communicator connects). passt's
    `addr/` prefix binds the whole comma-list, so it appears once -- repeating
    it (addr/a,addr/b) is an "Invalid port specifier" to passt.
    """
    by_proto: dict[str, list[str]] = {"tcp": [], "udp": []}
    for proto, host_port, guest_port in fwds:
        by_proto.setdefault(proto, []).append(f"{host_port}:{guest_port}")
    out: list[str] = []
    if by_proto["tcp"]:
        out += ["--tcp-ports", "127.0.0.1/" + ",".join(by_proto["tcp"])]
    if by_proto["udp"]:
        out += ["--udp-ports", "127.0.0.1/" + ",".join(by_proto["udp"])]
    return out


def _start_passt(sock: str, fwds: list[tuple[str, str, str]]) -> subprocess.Popen:
    """Launch the passt sidecar and block until its socket is listening."""
    cmd = [
        "passt",
        # Foreground so it stays a plain child (no daemon fork); logs to stderr
        # since the container has no syslog socket.
        "--foreground",
        "--quiet",
        # Quit once qemu (the only client) disconnects so a leaked sidecar can't
        # outlive its build VM. packer's process-group teardown is the backstop.
        "--one-off",
        "--socket",
        sock,
        *_passt_port_args(fwds),
    ]
    proc = subprocess.Popen(cmd, stdout=sys.stderr.fileno(), stderr=sys.stderr.fileno())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"qemu-net-wrapper: passt exited early (rc={proc.returncode}) before binding {sock}")
        if os.path.exists(sock):
            return proc
        time.sleep(0.05)
    proc.kill()
    sys.exit(f"qemu-net-wrapper: passt did not bind {sock} within 10s")


def main() -> None:
    args = sys.argv[1:]
    real_qemu = _real_qemu()

    override = os.environ.get("PACKER_NET_BACKEND", "auto").strip().lower()
    if override not in ("auto", "slirp", "passt"):
        sys.exit(f"qemu-net-wrapper: PACKER_NET_BACKEND={override!r} not in auto/slirp/passt")

    netdev_idx = _find_user_netdev(args)
    usable = _passt_usable(real_qemu)

    if override == "passt" and not usable:
        sys.exit("qemu-net-wrapper: PACKER_NET_BACKEND=passt but passt/`-netdev stream` is unusable here")

    # No user-netdev to rewrite (e.g. a `-version` probe), an explicit slirp
    # override, or passt not usable -> run real qemu untouched (slirp path).
    use_passt = override != "slirp" and usable and netdev_idx is not None
    if not use_passt:
        os.execv(real_qemu, [real_qemu, *args])

    netid, fwds = _parse_netdev_user(args[netdev_idx + 1])
    if netid is None or not fwds:
        # Couldn't make sense of packer's netdev; don't risk the build -- fall
        # back to the slirp arg packer already generated.
        os.execv(real_qemu, [real_qemu, *args])

    sock = os.path.join(tempfile.mkdtemp(prefix="packer-passt-"), "passt.sock")
    _start_passt(sock, fwds)

    args = list(args)
    args[netdev_idx + 1] = f"stream,id={netid},server=off,addr.type=unix,addr.path={sock}"
    os.execv(real_qemu, [real_qemu, *args])


if __name__ == "__main__":
    main()
