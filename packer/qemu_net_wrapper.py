#!/usr/bin/env python3
"""Packer qemu_binary shim that swaps libslirp for passt when available.

Packer emits a `-netdev user,...hostfwd=...` NIC for the build VM. In lab CI,
that nested slirp path can drop DNS traffic under parallel build load, so this
shim starts a passt sidecar and rewrites the netdev to qemu's stream socket
transport. Hosts without passt support, explicit PACKER_NET_BACKEND=slirp, and
probe invocations such as `qemu_binary -version` exec qemu unchanged.

Decision logs go to stderr and, when a build disk path is visible, to
qemu_net_wrapper.log beside the build artifacts. QEMU_NET_WRAPPER_LOG can point
elsewhere, or "off"/"0" can disable file logging.
"""

import datetime
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

# Resolved once in main() before any logging. Stays None when no build dir can
# be derived (e.g. packer's `-version` probe) -- stderr then carries the log
# alone, which is all PACKER_LOG would have surfaced anyway. _LOG_PATH is the
# file behind _LOG_FH, so _start_passt can drop passt's own debug log beside it.
_LOG_FH = None
_LOG_PATH = None
_WRAPPER_QEMU_BINARY_ARG = "-qemu-net-wrapper-binary"

# The nameserver passt advertises to the guest over DHCP (-D). This is the lab
# DNS keepalived VIP (data/network_topology.yml -> virtual_ips.dns): a
# real, reachable, split-horizon resolver that answers both nexus.lab.fahm.fr
# (internal) and archive.ubuntu.com (upstream), which the early base-image apt
# needs before the apt mirror is rewritten to nexus.
#
# Why a hard-coded real IP rather than passt's default: passt defaults to
# advertising the host's /etc/resolv.conf nameservers, but in the ci-container
# that's podman's loopback aardvark (127.0.0.11) -- unreachable from inside the
# guest, so every lookup fails (libslirp's built-in 10.0.2.3 forwarder masked
# this). --dns-forward can't rescue it either: passt forwards to the host's
# resolv.conf resolver, and it has no working path to a loopback upstream. So
# hand the guest a routable resolver instead and let passt NAT its queries out
# the container bridge -- the guest's DNS to <VIP>:53 then rides the same
# container -> VIP -> adguard DNAT path the firewall already permits (roles/
# firewall: "container -> VIP -> container" accept). The passt NIC path only
# runs in lab CI (a dev-Mac `packer:build` execs slirp untouched), so coupling
# to lab's resolver here is acceptable.
_PASST_DNS = "10.123.1.224"


def _log(msg: str) -> None:
    """Narrate one decision to stderr (PACKER_LOG path) and, when a build dir
    was found, to qemu_net_wrapper.log (the always-visible path)."""
    line = f"qemu-net-wrapper[{os.getpid()}]: {msg}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FH is not None:
        stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
        _LOG_FH.write(f"{stamp} {line}\n")
        _LOG_FH.flush()


def _open_log(args: list[str]) -> None:
    """Point _LOG_FH at a per-build-dir qemu_net_wrapper.log. The dir is the
    one packer feeds qemu its primary disk from (`-drive file=<dir>/packer-
    ubuntu,...`); QEMU_NET_WRAPPER_LOG overrides it or disables (off/0)."""
    global _LOG_FH, _LOG_PATH
    override = os.environ.get("QEMU_NET_WRAPPER_LOG", "").strip()
    if override.lower() in ("off", "0", "none"):
        return
    path = override or None
    if path is None:
        build_dir = _build_dir_from_args(args)
        if build_dir is None:
            return
        path = os.path.join(build_dir, "qemu_net_wrapper.log")
    try:
        _LOG_FH = open(path, "a", encoding="utf-8")
        _LOG_PATH = path
    except OSError as exc:
        # A missing/unwritable sink must never sink the build -- stderr still
        # carries the log under PACKER_LOG.
        print(f"qemu-net-wrapper: could not open log {path!r}: {exc}", file=sys.stderr, flush=True)


def _build_dir_from_args(args: list[str]) -> str | None:
    """Directory packer writes the build VM's disks into, read off the `-drive
    file=<dir>/packer-ubuntu...` arg. None when there's no such drive (a probe
    invocation), which keeps file logging off where there's nothing to trace."""
    for arg in args:
        for part in arg.split(","):
            if part.startswith("file="):
                disk = part[len("file=") :]
                if os.path.basename(disk).startswith("packer-ubuntu"):
                    parent = os.path.dirname(disk)
                    if parent and os.path.isdir(parent):
                        return parent
    return None


def _real_qemu(args: list[str]) -> tuple[str, list[str]]:
    configured = None
    stripped = []
    i = 0
    while i < len(args):
        if args[i] == _WRAPPER_QEMU_BINARY_ARG:
            try:
                configured = args[i + 1]
            except IndexError:
                sys.exit(f"qemu-net-wrapper: {_WRAPPER_QEMU_BINARY_ARG} needs a value")
            i += 2
            continue
        stripped.append(args[i])
        i += 1

    if configured is None:
        arch = platform.machine()
        arch = "aarch64" if arch == "arm64" else arch
        configured = f"qemu-system-{arch}"
    binary = shutil.which(configured)
    if binary is None:
        sys.exit(f"qemu-net-wrapper: {configured} not found on PATH")
    return binary, stripped


def _passt_usable(real_qemu: str) -> bool:
    """True when passt is on PATH and this qemu advertises `-netdev stream`."""
    host_os = platform.system()
    if host_os != "Linux":
        _log(f"passt unusable: host is {host_os}, not Linux")
        return False
    if shutil.which("passt") is None:
        _log("passt unusable: no `passt` on PATH")
        return False
    try:
        probe = subprocess.run(
            [real_qemu, "-netdev", "help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"passt unusable: `{real_qemu} -netdev help` probe failed: {exc}")
        return False
    has_stream = "stream" in (probe.stdout + probe.stderr)
    _log(f"passt probe: binary present, qemu `-netdev stream` {'supported' if has_stream else 'absent'}")
    return has_stream


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


def _start_passt(sock: str, fwds: list[tuple[str, str, str]]) -> None:
    """Launch the passt sidecar and block until its socket is listening."""
    cmd = [
        "passt",
        # Foreground so it stays a plain child (no daemon fork).
        "--foreground",
        # Quit once qemu (the only client) disconnects so a leaked sidecar can't
        # outlive its build VM. packer's process-group teardown is the backstop.
        "--one-off",
        "--socket",
        sock,
        # Advertise a routable resolver to the guest over DHCP; passt then NATs
        # the guest's DNS queries out the container bridge to it. See _PASST_DNS
        # for why passt's default (the container's loopback aardvark) is useless
        # here and why --dns-forward can't bridge to it.
        "--dns",
        _PASST_DNS,
        *_passt_port_args(fwds),
    ]
    # Capture passt's startup banner (which echoes the DHCP-advertised DNS and
    # the bound socket -- the diagnostic for a DNS-path regression) into a log
    # beside ours. We must NOT pass passt `--log-file <path>`: passt's apparmor
    # profile denies open() on an arbitrary path, so it exits 1 before binding
    # the socket and qemu's `-netdev stream` connect then fails ("Qemu failed
    # to start"). Instead the wrapper opens the file and hands passt the fd as
    # its stdout/stderr -- apparmor mediates open() by path, not writes to an
    # inherited fd. The file is a sibling of _LOG_PATH (outside any per-source
    # output dir), so it survives packer's failed-build delete; build.sh dumps
    # both. No --debug: it adds a per-packet trace that would bloat the job log
    # during the image transfer; the banner alone shows the DNS wiring. Without
    # a sink, stay --quiet -> wrapper stderr, which packer discards unless
    # PACKER_LOG is set.
    passt_out = sys.stderr.fileno()
    if _LOG_PATH is not None:
        passt_log = f"{_LOG_PATH}.passt-{os.getpid()}"
        try:
            passt_out = os.open(passt_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError as exc:
            _log(f"could not open passt log {passt_log!r}: {exc}; passt output -> wrapper stderr")
            cmd.append("--quiet")
    else:
        cmd.append("--quiet")
    _log(f"starting passt sidecar: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=passt_out, stderr=passt_out)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"qemu-net-wrapper: passt exited early (rc={proc.returncode}) before binding {sock}")
        if os.path.exists(sock):
            _log(f"passt sidecar (pid={proc.pid}) bound {sock}")
            return
        time.sleep(0.05)
    proc.kill()
    sys.exit(f"qemu-net-wrapper: passt did not bind {sock} within 10s")


def main() -> None:
    real_qemu, args = _real_qemu(sys.argv[1:])
    _open_log(args)

    override = os.environ.get("PACKER_NET_BACKEND", "auto").strip().lower()
    if override not in ("auto", "slirp", "passt"):
        sys.exit(f"qemu-net-wrapper: PACKER_NET_BACKEND={override!r} not in auto/slirp/passt")

    netdev_idx = _find_user_netdev(args)
    _log(
        f"invoked: real_qemu={real_qemu}, PACKER_NET_BACKEND={override}, "
        f"user-netdev {'found' if netdev_idx is not None else 'absent'}, {len(args)} args"
    )
    usable = _passt_usable(real_qemu)

    if override == "passt" and not usable:
        sys.exit("qemu-net-wrapper: PACKER_NET_BACKEND=passt but passt/`-netdev stream` is unusable here")

    # No user-netdev to rewrite (e.g. a `-version` probe), an explicit slirp
    # override, or passt not usable -> run real qemu untouched (slirp path).
    use_passt = override != "slirp" and usable and netdev_idx is not None
    if not use_passt:
        _log(
            "backing build-VM NIC with slirp (passthrough): "
            + (
                "forced by override"
                if override == "slirp"
                else "no user-netdev to rewrite" if netdev_idx is None else "passt unusable here"
            )
        )
        os.execv(real_qemu, [real_qemu, *args])

    netid, fwds = _parse_netdev_user(args[netdev_idx + 1])
    if netid is None or not fwds:
        # Couldn't make sense of packer's netdev; don't risk the build -- fall
        # back to the slirp arg packer already generated.
        _log(
            f"backing build-VM NIC with slirp (passthrough): unparseable netdev {args[netdev_idx + 1]!r} (id={netid}, fwds={fwds})"
        )
        os.execv(real_qemu, [real_qemu, *args])

    _log(f"backing build-VM NIC with passt: netdev id={netid}, host-forwards={fwds}")
    sock = os.path.join(tempfile.mkdtemp(prefix="packer-passt-"), "passt.sock")
    _start_passt(sock, fwds)

    args = list(args)
    args[netdev_idx + 1] = f"stream,id={netid},server=off,addr.type=unix,addr.path={sock}"
    _log(f"rewrote netdev to: {args[netdev_idx + 1]}; exec {real_qemu}")
    os.execv(real_qemu, [real_qemu, *args])


if __name__ == "__main__":
    main()
