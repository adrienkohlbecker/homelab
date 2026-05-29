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
reaches the guest at 127.0.0.1:<port>. The sidecar also runs a DNS proxy
(--dns/--dns-forward, see _PASST_DNS) because passt -- unlike libslirp's built-in
forwarder -- won't relay the ci-container's loopback resolver to the guest. On a
host without passt (a dev-Mac `mise run packer:build`, or any qemu < 7.2), it
execs qemu unchanged, so the slirp path stays byte-for-byte identical.

Wired in via `qemu_binary` in packer/qemu.pkr.hcl. The arch's real emulator is
resolved from PATH here (arch is 1:1 with the host, same assumption the HCL
arch_table makes), so one shim serves both x86_64 and aarch64.

Set PACKER_NET_BACKEND={auto,slirp,passt} to override the probe (mirrors the
harness's HOMELAB_NET_BACKEND): `slirp` forces passthrough, `passt` fails loudly
if passt is unusable, `auto` (default) follows the capability probe.

Diagnostics: the shim narrates every decision (probe result, override,
slirp-vs-passt choice, the passt command + socket, the netdev rewrite) so a
build that takes the wrong NIC path is debuggable. packer routes a
qemu_binary's stderr through Go's logger, which it discards unless PACKER_LOG
is set -- so the shim ALSO drops a qemu_net_wrapper.log into the per-source
build dir (the same dir holding packer-ubuntu*, derived from the `-drive
file=` arg). That file rides the shared /mnt/scratch/qemu volume in CI and is
tailed by mise-tasks/packer/build.sh after each build, so the decision trail
reaches the job log without PACKER_LOG. Override the path (or disable with
"off"/"0") via QEMU_NET_WRAPPER_LOG.
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

# A reserved TEST-NET-1 address (RFC 5737, never routed) used purely as the
# guest's nameserver. The guest is told (DHCP, -D) to send DNS here; passt
# intercepts traffic to --dns-forward and proxies it to the *host's* resolvers
# (the ci-container's /etc/resolv.conf -> podman's 127.0.0.11 aardvark, which
# resolves both nexus.lab.fahm.fr and upstream archive.ubuntu.com). Without
# this, passt hands the guest the container's loopback 127.0.0.11 verbatim --
# unreachable from inside the guest -- so every guest lookup fails. libslirp's
# built-in forwarder (10.0.2.3) masked this; passt forwards nothing by default.
_PASST_DNS = "192.0.2.1"


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
        _log(f"passt unusable: host is {platform.system()}, not Linux")
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


def _start_passt(sock: str, fwds: list[tuple[str, str, str]]) -> subprocess.Popen:
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
        # Give the guest a resolver it can actually reach: advertise _PASST_DNS
        # via DHCP (--dns) and have passt proxy queries sent there to the host's
        # resolvers (--dns-forward). See _PASST_DNS for why the container's own
        # loopback resolver can't be handed through.
        "--dns",
        _PASST_DNS,
        "--dns-forward",
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
            return proc
        time.sleep(0.05)
    proc.kill()
    sys.exit(f"qemu-net-wrapper: passt did not bind {sock} within 10s")


def main() -> None:
    args = sys.argv[1:]
    _open_log(args)
    real_qemu = _real_qemu()

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
