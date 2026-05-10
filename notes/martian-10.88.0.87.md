# Martian floods to `10.88.0.87` (no container owns it)

## Symptom

`lab` kernel log:

```
IPv4: martian source 10.88.0.87 from <X>, on dev eth0
```

~13–14 k entries/day across many `<X>` source IPs. Reading: dst =
`10.88.0.87`, src = `<X>` (kernel format prints `daddr` first; the
`source` wording is misleading).

## What I confirmed on lab

- **No container has or had `10.88.0.87`** — `podman ps -a` /
  `podman inspect` across all containers shows no match. `ip neigh`
  shows no entry. `nft -a list ruleset` and `iptables-save` contain
  no DNAT rule mentioning `.87`.
- **Repo has zero references to `10.88.0.87`** — `grep -rn` across
  `roles/`, `terraform/`, `group_vars/`, `host_vars/`. So this is
  not in any unit template or DNS zone we manage.
- **Source IPs span LAN, sibling VLANs, and the public internet:**

  | Sources (6 h sample) | Count | Comment |
  | --- | --- | --- |
  | `10.123.2.7` | 6419 | host on `10.123.2/24` (other VLAN) |
  | `10.123.2.6` | 2074 | same VLAN |
  | `10.123.2.2` | 1594 | same VLAN |
  | `10.123.0.1` | 892 | **the upstream router itself** |
  | `10.123.0.2` | 809 | **lab itself** |
  | `10.123.2.246` | 671 | same VLAN |
  | `171.33.72.166`, `139.162.71.178` | 1100+ | Linode public IPs |
  | `172.67.68.90`, `104.26.4.238` | ~100 | Cloudflare public IPs |

- **L2 source MAC of every captured frame is locally-administered**
  (`4a:72:53:97:70:8d` and similar) — consistent with a virtual
  upstream router interface (CARP/VRRP/keepalived virtual MAC), not
  a physical device on the LAN.

## Most likely cause

The upstream router has a **stale static route** of the form
`10.88.0.0/16 via 10.123.0.2` (lab) and **a stale port-forward / DNAT
rule** that targets `10.88.0.87` directly. Anything that resolves /
hits that DNAT — LAN clients, the router doing health checks, internet
clients via the public IP — gets forwarded onto lab as
`dst=10.88.0.87`. Lab has no container at `.87`, so RP-filter trips on
delivery against the eth0 ingress and the kernel logs each frame as
martian. The `10.123.0.1` (router) and `10.123.0.2` (lab itself)
sources strongly suggest a health-checker hitting the dead IP.

The volume from public IPs (Linode, Cloudflare) implies a published
service was once at `.87` and external clients (or cached upstream
DNS) still find it.

## Why this doesn't break anything

It's loud but harmless:

- The packets get dropped at L3 by the kernel.
- The host has no service at `.87`, so nothing is misrouted.
- No reply path exists, so external clients don't loop.

Cost is purely log volume (~10× the homeassistant collision).

## Next steps (need router access)

1. **On the upstream router** (probably the `10.123.0.1` gateway):
   - Look for a static route `10.88.0.0/16 via 10.123.0.2` and delete
     or shrink it to only the IPs that actually exist (currently
     `10.88.0.{1,2,76,...}`).
   - Look for a port-forward / DNAT rule with `10.88.0.87` as the
     destination and remove it (or repoint at the live IP, which is
     pihole at `10.88.0.76`).
2. **Optional, on lab**: silence the logs in the meantime with
   `sysctl net.ipv4.conf.eth0.log_martians=0` or by adding a rule to
   `chain prerouting` in [nftables.conf.j2](../roles/firewall/templates/nftables.conf.j2)
   that drops `ip daddr 10.88.0.87` early. Don't apply this until the
   root cause is confirmed — silencing is information-loss otherwise.
3. **Audit other ghost IPs**: the same router config might still have
   stale entries pointing at other former container IPs. After fixing
   `.87`, watch `journalctl -k --since 1h | grep martian | sort | uniq -c`
   for anything else.

## What container *was* `10.88.0.87`?

Unknown. Netavark assigns IPs from `10.88.0.0/16` in roughly the
order containers come up, so `.87` was somewhere in the middle of the
default-bridge container set at some point. It may have been pihole
before a recreate (pihole is now `.76`) or some other web service that
got published externally. Without netavark history (it doesn't keep
one) we can only guess.
