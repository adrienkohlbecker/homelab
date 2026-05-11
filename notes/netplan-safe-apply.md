# Netplan safe-apply pattern

Background — why the role applies netplan through three nested safety
gates instead of a plain `netplan apply`:

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
general problem than those two specifics, so the role now wraps apply
in a safety harness.

## The harness

Three independent layers, each addressing a different failure mode:

### 1. Pre-flight validation
`netplan generate --debug` compiles the YAML and writes to
`/run/systemd/network/` *without* restarting networkd. We grep its
output for `WARNING|Cannot find unique|ERROR` and refuse to apply if
any of those appear. Catches the unique-match class of bug before it
touches live state.

### 2. `netplan apply --state <snapshot>`
Before apply we snapshot `/etc/netplan` to `/var/lib/netplan-prev` and
pass that path via `--state`. In theory netplan diffs new-vs-state and
restricts the restart scope to genuinely-changed ifaces. The flag is
under-documented and the practical effect varies between netplan
versions, but it's cheap to pass and never hurts. Don't rely on this
as the only safety net — layer 3 is the real backstop.

### 3. systemd-run-scheduled rollback (the real backstop)
Before apply:
- Snapshot `/etc/netplan` to `/run/netplan-rollback`.
- `systemd-run --on-active=90 --unit=netplan-rollback --collect` arms
  a one-shot transient timer-unit that fires in 90 s.

After apply, ansible's `wait_for_connection` (timeout: 60 s) probes
SSH. If it reconnects, ansible `touch`es `/run/netplan-keep`. When
the timer fires at +90 s it reads that file: present = cancelled,
absent = roll back (mv `/run/netplan-rollback` → `/etc/netplan`,
`netplan apply`).

The 90 s vs 60 s gap is deliberate: a slow reconnect mustn't be
mis-read as a failed apply.

`--collect` makes systemd clean up the transient unit after it runs,
so successive applies don't accumulate dead units. `logger -t
netplan-rollback` writes the outcome to the journal so post-mortems
can tell whether the rollback fired.

## What this does NOT protect against

- **Reboot-during-bad-apply.** `/run/netplan-rollback` lives in tmpfs;
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
  fire (we reconnected), so the bad config sticks. Validation in
  layer 1 catches typos/structural errors but not semantic ones.

## Operator tips

- For interactive work from the console (where you can press Enter),
  prefer `netplan try --timeout 60`. That's the upstream-blessed safe
  apply path; it just needs a TTY, which ansible doesn't have.
- Run netplan changes over wireguard (`wg0`) rather than the eth0 LAN
  path. wg0 doesn't go through the eth0 churn during apply.
- After any non-trivial netplan change, immediately verify from a
  fresh shell — the connection ansible used can give a false-positive
  "still alive" while the link is actually only partially up.
