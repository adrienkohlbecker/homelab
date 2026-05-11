# Netplan safe-apply pattern

Background — why the role applies netplan through a snapshot-and-rollback
harness instead of a plain `netplan apply`:

## The incident (2026-05-10)

Cosmetic edit to `host_vars/lab.yml`: switched two inactive i225 NICs
from `dhcp4: true` to `dhcp4: false`. Generated YAML written, ansible
ran `netplan apply` over SSH from a workstation. SSH died and stayed
dead until a physical-console reboot.

What actually happened, in order:

1. `netplan apply` triggered a full networkd reload. networkd doesn't
   diff `.network` files — even unchanged ifaces are re-read and
   re-applied, which carrier-flaps the underlying NIC. eth0's DHCP
   lease was momentarily lost / re-acquired, severing the TCP/SSH
   connection in flight.
2. Netplan's apply-time matcher walks `/sys/class/net` using the
   *runtime* MAC (not `PermanentMACAddress` like the generated
   `.network` files). The `iot` VLAN dev inherits eth0's MAC, so the
   `nic0` block matched both eth0 and iot — producing
   `WARNING:root:Cannot find unique matching interface for nic0` and
   halting the apply mid-way. The box came back up healthy only on
   the next reboot because runtime networkd state was inconsistent
   after the failed apply.

Fixes landed for those two specific bugs:
- `host_vars/lab.yml` now pins each ethernet block with
  `match.driver: igb`/`igc` so the matcher only catches the physical
  NIC, never an MAC-inheriting VLAN dev.
- `host_vars/lab.yml` netplan no longer carries `dhcp4: true` on
  inactive NICs (activation-mode:off is not enough — transient
  carrier events still grab leases).

But "a careless netplan edit can lock you out of the box" is a more
general problem than those two specifics, so the role wraps apply in
a safety harness.

## The harness

A single snapshot, taken *before* the role's template overwrites
`/etc/netplan`, feeds two consumers:

### Snapshot timing — load-bearing

The first revision of the harness snapshotted `/etc/netplan` *inside*
the apply block (after the `Configure netplan` template task had
already written the new config). That snapshot captured the new
config, so:
- `netplan apply --state` was diffing new-against-new and never
  restricted restart scope.
- The rollback timer's `mv /run/netplan_prev /etc/netplan` would
  have restored the new (broken) config and re-applied it.

Both broken silently because `_verify` only asserted wiring (snapshot
dir exists, rollback unit reaped) and never triggered an actual
rollback.

Fixed in commit `7b015c0d` by moving the snapshot above the template
task and running it unconditionally every converge. `/run/netplan_prev`
now holds the genuine *previous* `/etc/netplan` for both consumers.

### Consumer 1: `netplan apply --state /run/netplan_prev`

`--state` lets netplan diff the prior YAML against the new YAML in
`/etc/netplan` and restrict the networkd restart scope to genuinely
changed interfaces. Reduces carrier-flap blast radius on unrelated
NICs.

### Consumer 2: timer-armed rollback (the real backstop)

The role installs three persistent files once at apply-time:
- `/usr/local/bin/netplan_rollback` — the rollback shell script.
- `/etc/systemd/system/netplan_rollback.service` — oneshot, ExecStart
  the script.
- `/etc/systemd/system/netplan_rollback.timer` — OnActiveSec=90,
  Unit=netplan_rollback.service.

Before apply, the role removes any stale `/run/netplan_keep` and does
`systemd: state=restarted` on `netplan_rollback.timer`, which (re)sets
the 90 s deadline. After apply, ansible's `wait_for_connection`
(timeout: 60 s) probes SSH. If it reconnects, ansible `touch`es
`/run/netplan_keep` and stops the timer. When the timer fires at
+90 s — only relevant on the SSH-died path — the script reads
`/run/netplan_keep`: present = cancelled (no-op), absent = roll back
(`mv /run/netplan_prev /etc/netplan`, `netplan apply`).

The 90 s vs 60 s gap is deliberate: a slow reconnect mustn't be
mis-read as a failed apply.

`logger -t netplan_rollback` writes the outcome to the journal so
post-mortems can tell whether the rollback fired. The previous
revision used `systemd-run --on-active=90 ... --collect` for the
transient-unit equivalent of this pattern, but persistent unit files
are easier to audit and let `_verify` exercise the same path via the
`systemd:` module without re-inventing systemd-run semantics in
shell.

### Why one snapshot directory, not two

The two consumers' access windows don't overlap in practice:
- `netplan apply` (Consumer 1) reads `--state` at the start of apply
  and is done within seconds.
- The timer (Consumer 2) fires at +90 s, well after Consumer 1 has
  finished.

The earlier two-directory split was defensive against a race that
doesn't exist; the merge (commit `7b015c0d`) keeps the rollback path
correct while halving the snapshot I/O and cleanup.

The Cancel step intentionally does NOT remove `/run/netplan_prev` —
the next converge's pre-template snapshot rotates it.

## What this does NOT protect against

- **Reboot-during-bad-apply.** `/run/netplan_prev` lives in tmpfs;
  the systemd-run timer is in runtime state. If the box reboots between
  apply and the timer firing, the timer is lost and the new (potentially
  broken) `/etc/netplan` becomes effective on next boot. Operator still
  needs console access in that scenario.
- **Connectivity over a different path.** `wait_for_connection` from
  the ansible host is a reasonable canary, but if the operator is on
  wireguard or a side channel that survives eth0 churn, ansible may
  report success while a downstream LAN client is broken.
- **netplan apply succeeds but produces wrong-but-reachable config.**
  E.g. wrong default route via the wrong iface. The rollback won't
  fire (we reconnected), so the bad config sticks.

## Operator tips

- For interactive work from the console (where you can press Enter),
  prefer `netplan try --timeout 60`. That's the upstream-blessed safe
  apply path; it just needs a TTY, which ansible doesn't have.
- Run netplan changes over wireguard (`wg0`) rather than the eth0 LAN
  path. wg0 doesn't go through the eth0 churn during apply.
- After any non-trivial netplan change, immediately verify from a
  fresh shell — the connection ansible used can give a false-positive
  "still alive" while the link is actually only partially up.
