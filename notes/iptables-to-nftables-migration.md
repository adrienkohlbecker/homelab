# iptables → nftables migration

## Why bother

Three concrete wins, in descending order of value:

1. **No more wipe-and-reload cascade.** `iptables-restore` flushes every chain in every table the file touches — so any third party that adds rules at runtime (netavark, libvirt, fail2ban) needs a stat-gated reload after we apply (`roles/iptables/tasks/main.yml` lines 56–127). nftables is per-table atomic: `nft -f /etc/nftables.conf` replaces only the tables our file declares. Other tables are untouched. The four reload tasks go away.

2. **Named sets collapse the LAN+WG rule pairs.** Today every admin-scoped INPUT rule is duplicated because iptables `-s` takes one CIDR (`rules.v4.j2` lines 41–42, 62–65). With an `lan_admin_sources` set the four-rule pair (samba LAN+WG, nut LAN+WG) becomes two single rules referencing `@lan_admin_sources`. Adding a future source CIDR is one set-element edit, not a four-rule fan-out.

3. **Atomic apply.** No "rules are half-loaded if iptables-restore crashes mid-file" failure mode. `nft -f` parses the entire ruleset, validates it, then commits in one transaction.

Smaller wins: `nft -j list ruleset` emits JSON (cleaner counter parsing in `_verify.yml` than the regex over `iptables-save -c` we have now); explicit-deny blocks become one rule with a port set (`udp dport { 67, 137, 138, 139, 5351, 10001, 17500, 57621 } reject`); `inet` family lets one ruleset cover ip4+ip6 (we don't care, v6 is kernel-disabled, but worth mentioning).

Cost: substantial `_verify.yml` rewrite (counter assertions change tooling), prod cutover with rollback contingency, mild learning curve.

## End-state shape

### `roles/iptables/templates/nftables.conf.j2`

The full ruleset in one file, keyed off the same vars as `rules.v4.j2`. Family is `ip` (matches our v4-only world; `inet` is also fine but adds a dimension we don't use).

```nft
#!/usr/sbin/nft -f

# Atomic replace of just our table — netavark / libvirt / fail2ban
# tables in the same kernel ruleset are untouched.
flush table ip homelab
table ip homelab {

    # Admin services scoped to home LAN + wireguard. Adding a future
    # site (brumath, bonniers) is one set-element edit.
    set lan_admin_sources {
        type ipv4_addr
        flags interval
        elements = { {{ site_subnets.home }}, {{ wireguard_subnet }} }
    }

    set mosh_sources {
        type ipv4_addr
        flags interval
        elements = { {{ site_subnets.home }}, {{ wireguard_subnet }} }
    }

    chain input {
        type filter hook input priority filter; policy drop;

        ct state established,related accept
        iif lo accept
        ct state invalid drop

        # Mirror of the iptables ! --syn drop — see rules.v4.j2 for
        # the conntrack tcp_loose rationale.
        tcp flags & (fin|syn|rst|ack) != syn counter drop

        # UDP allows.
        ip saddr {{ podman_default_network }} udp dport 5053 accept comment "dnscrypt-proxy (containers)"
        ip saddr {{ podman_default_network }} udp dport 5333 accept comment "aardvark-dns"
        ip saddr {{ site_subnets.home }} udp dport 5353 accept comment "mDNS (home LAN)"
        iifname "{{ ansible_default_ipv4.interface }}" udp dport 51820 accept comment "wireguard (WAN ingress only)"
        ip saddr @mosh_sources udp dport 60000-61000 accept comment "Mosh"

        # Explicit denies — port set replaces 7 rules.
        udp dport { 67, 137, 138, 139, 5351, 10001, 17500, 57621 } reject with icmp type port-unreachable

        # TCP allows. Wide-open vs. admin-scoped.
        tcp dport { 22, 80, 443 } accept
        ip saddr @lan_admin_sources tcp dport { 445, 3493 } accept

        # ICMP echo-request rate-limited.
        icmp type echo-request limit rate 10/second burst 20 packets accept
        icmp type echo-request drop

        # VRRP for keepalived.
        ip protocol 112 accept comment "keepalived (VRRP)"

        # Catch-all reject.
        limit rate 5/minute burst 10 packets log prefix "[nftables] INPUT:REJECT: "
        meta l4proto udp reject with icmp type port-unreachable
        meta l4proto tcp reject with tcp reset
        reject with icmp type prot-unreachable
    }

    chain forward {
        type filter hook forward priority filter; policy drop;

        ct state established,related accept
        ct state invalid drop
        tcp flags & (fin|syn|rst|ack) != syn counter drop

        # Egress paths from internal interfaces to WAN.
        iifname "wg0" oifname "{{ ansible_default_ipv4.interface }}" accept comment "wg→WAN"
        iifname "podman0" oifname "{{ ansible_default_ipv4.interface }}" accept comment "podman→WAN"

        # Reach published containers via netavark's DNAT.
        iifname { "{{ ansible_default_ipv4.interface }}", "wg0" } oifname "podman0" \
            ip daddr {{ podman_default_network }} ct status dnat accept comment "Reach published containers"

        limit rate 5/minute burst 10 packets log prefix "[nftables] FORWARD:REJECT: "
        counter drop
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr {{ podman_default_network }} oifname "{{ ansible_default_ipv4.interface }}" masquerade comment "NAT container egress"
        ip saddr {{ wireguard_subnet }} oifname "{{ ansible_default_ipv4.interface }}" masquerade comment "NAT wireguard-client egress"
    }
}
```

Notes on the translation:
- `! --syn` → `tcp flags & (fin|syn|rst|ack) != syn` — same semantics, slightly less readable but functionally identical. Kept the `counter` keyword on this and on the FORWARD catch-all DROP because `_verify.yml` asserts these specific drops fire.
- `--ctstate DNAT` → `ct status dnat`. `ct state` is for connection lifecycle (NEW/ESTABLISHED); `ct status` is for "did some chain DNAT this packet". Distinct keywords; easy to mix up.
- The combined ingress-iface match `iifname { "WAN", "wg0" }` collapses two iptables rules into one with a name set — not a CIDR set, but the same idea.
- `comment "..."` lands inline in `nft list ruleset` output; we don't need the `-m comment --comment` boilerplate.
- `ip protocol 112` → VRRP. Could write `ip protocol vrrp` if nft's keyword list includes it on Ubuntu's version (it does on 1.0.6+).

### `roles/iptables/tasks/main.yml`

The reload-cascade goes away entirely. Final shape:

```yaml
- name: Install nftables
  apt:
    pkg:
      - nftables
    cache_valid_time: 3600
  become: true

- name: Install network-stack hardening sysctls
  copy:
    src: sysctl-hardening.conf
    dest: /etc/sysctl.d/30-iptables-hardening.conf
    owner: root
    group: root
    mode: "0644"
    backup: true
  register: iptables_sysctl
  become: true

- name: Apply hardening sysctls
  when: iptables_sysctl.changed
  command: sysctl -p /etc/sysctl.d/30-iptables-hardening.conf
  changed_when: true
  become: true

- name: Configure nftables ruleset
  template:
    src: nftables.conf.j2
    dest: /etc/nftables.conf
    owner: root
    group: root
    mode: "0644"
    backup: true
    validate: "nft -c -f %s"
  register: nftables_ruleset
  become: true

- name: Apply nftables ruleset
  when: nftables_ruleset.changed
  command: nft -f /etc/nftables.conf
  changed_when: true
  become: true

- name: Enable nftables.service
  systemd:
    name: nftables.service
    enabled: true
    state: started
  become: true

# One-shot cleanup of the previous regime — drop after a release cycle.
- name: Remove iptables-persistent
  apt:
    pkg:
      - iptables-persistent
    state: absent
  become: true

- name: Remove legacy iptables rule files
  file:
    path: "{{ item }}"
    state: absent
  loop:
    - /etc/iptables/rules.v4
    - /etc/iptables/rules.v6
    - /etc/iptables
  become: true
```

What disappeared (~70 lines):
- `iptables-persistent` install
- `Configure iptables (v4)` template + `Apply iptables v4` restore
- `Check for podman binary` + `Reload netavark/podman firewall rules after restore`
- `Check for virsh binary` + `List active libvirt networks` + `Reload libvirt networks`
- `Check for fail2ban-client binary` + `Reload fail2ban`
- The whole rules.v6 dance (already gone in 7c36d0d9)

Critical gain: `nft -c -f` validates the file *before* we apply it — `iptables-restore --test` exists but the role doesn't currently use it. Free correctness improvement.

### `roles/iptables/tasks/_verify.yml`

The functional probes (curl/nc through netns, delegate_to localhost via WAN hostfwds) don't change — they exercise *behavior*, not framework internals. The two counter-based assertions need to swap tooling:

```yaml
- name: _verify | Capture nftables counters after crafted-packet probes
  command: nft -j list table ip homelab
  register: nft_counters_after
  changed_when: false
  become: true

- name: _verify | INPUT — non-SYN ACK was dropped
  vars:
    rules: "{{ (nft_counters_after.stdout | from_json).nftables | selectattr('rule', 'defined') | map(attribute='rule') | list }}"
    ack_drop_counter: >-
      {{ rules | selectattr('chain', 'equalto', 'input')
                | selectattr('expr', 'defined')
                | selectattr('comment', 'undefined')
                | map(attribute='expr')
                | select('contains', {'match': {'op': '!=', 'left': {'payload': {'protocol': 'tcp', 'field': 'flags'}}}})
                | list | first }}
  assert:
    that: ack_drop_counter.counter.packets > 0
    fail_msg: "non-SYN ACK probe didn't bump the INPUT ! --syn counter"
```

This is uglier than the iptables-save regex it replaces, but `from_json` + filter is structurally sound where the regex was fragile (it broke once already on the `-m comment` insertion order). Could also fall back to `nft list table ip homelab` text + grep, which is simpler if less defensible.

The `iptables -Z` zeroing call becomes `nft reset counters table ip homelab`. Same semantics.

## Migration mechanics

Three phases, each independently revertible.

### Phase 1: Author and harness-validate the new ruleset

- Write `nftables.conf.j2` and the new `_verify.yml` counter section.
- Don't touch `main.yml` yet — keep iptables in charge.
- New harness path: `test/testrole.py iptables --machine box` against a forked role copy (`roles/nftables/`) that *only* installs and applies the new ruleset, alongside the existing iptables-managed rules. nftables and iptables-nft live in separate kernel tables; both apply, last-rule-wins on the chain hooks they share. Run `_verify.yml` against the nftables ruleset specifically.
- Validates: rule semantics, counter parsing, set ergonomics. No production exposure.

Estimated: 4–6h.

### Phase 2: Cutover in `roles/iptables`

- Replace `main.yml` body per the sketch above.
- The "remove iptables-persistent" task is the irreversible step. It runs after the new ruleset has loaded, so there's a brief window of double-coverage.
- Run on `box` first via the test harness (`test/testrole.py iptables`), then `lab` and `pug` variants, then a single non-critical prod host (eaton or marantz), then the rest with `--limit` rolling out one at a time.
- Watch netavark / libvirt / fail2ban: a `podman network reload --all` after cutover is a sanity belt-and-suspenders, but it shouldn't be load-bearing.

Estimated: 2–3h apply + bake.

### Phase 3: Cleanup

- After ~a week without incident, remove the "Remove iptables-persistent" cleanup tasks from `main.yml` (they'll be no-ops on every host by then).
- Rename the role? `roles/iptables` → `roles/firewall` is more honest but breaks every importer. Defer indefinitely; it's just a name.

### Phase 4: Migrate adjacent firewall consumers

Three integrations write rules at runtime alongside ours: netavark, libvirt, fail2ban. After phase 2 they all keep working unchanged — they sit in `ip filter` / `ip nat` (the iptables-nft compat tables), independent of our `ip homelab`. The migrations below are *cleanup*, not requirements: they consolidate everything onto native nft tooling so `nft list ruleset` is the one introspection point and the iptables-nft compat shim becomes unused. Each is independently revertible and unblocks nothing else.

#### Phase 4a: netavark → native nft driver

Modern netavark (1.10+, Ubuntu 24.04 noble ships 1.12+) supports a native nftables firewall driver via `containers.conf`. Today netavark detects iptables-nft and uses it; flipping the driver makes it create rules in its own `inet netavark` table instead of the iptables-compat `ip filter` / `ip nat` chains.

```yaml
# roles/podman/tasks/main.yml or a new firewall-config block
- name: Configure netavark to use native nftables
  copy:
    dest: /etc/containers/containers.conf.d/10-firewall-driver.conf
    content: |
      [network]
      firewall_driver = "nftables"
    mode: "0644"
    backup: true
  register: netavark_firewall_driver
  become: true

- name: Reload netavark with new driver
  when: netavark_firewall_driver.changed
  command: podman network reload --all
  changed_when: true
  become: true
```

Validation: after apply, `nft list tables` shows `inet netavark`; `iptables -L NETAVARK_FORWARD` returns "No such chain". Brief connectivity blip during reload (sub-second per network).

Risk: containers running during the cutover lose ingress until `network reload` finishes. Schedule on a low-traffic window; published ports go down for ~1–2s.

Effort: 1h including verify.

#### Phase 4b: libvirt → native nft backend (release-gated)

Libvirt grew a `firewall_backend` config in 10.4 (May 2024). Set it in `/etc/libvirt/network.conf`:

```yaml
- name: Configure libvirt to use native nftables backend
  lineinfile:
    path: /etc/libvirt/network.conf
    regexp: '^#?firewall_backend\s*='
    line: 'firewall_backend = "nftables"'
    create: true
    backup: true
  register: libvirt_firewall_backend
  become: true

- name: Restart libvirtd for new backend
  when: libvirt_firewall_backend.changed
  systemd:
    name: libvirtd
    state: restarted
  become: true
```

After restart: `virsh net-destroy default && virsh net-start default` for each network re-installs rules under the new backend. Validation: `nft list tables` shows `inet libvirt_*`; `iptables -L LIBVIRT_INP` returns "No such chain".

**Release-gated.** Ubuntu 24.04 noble ships libvirt 10.0.0 — backend flag accepted but driver not present. Need 10.4+, which lands in noble-backports or in the next LTS (26.04). Until then this phase is a no-op task: leave libvirt on iptables-nft compat. Re-evaluate on every base-image bump (`mise run packer:build --ubuntu <codename>`).

Effort: 30min once available; 0 today.

#### Phase 4c: fail2ban → nftables banaction

Default `banaction` on Ubuntu is `iptables-multiport` (writes via iptables-nft into `ip filter`'s `f2b-*` chains and an INPUT jump). The package ships nftables equivalents in `/etc/fail2ban/action.d/nftables-*.conf` — flip via a single config drop-in.

```yaml
# roles/fail2ban/tasks/main.yml addition
- name: Use nftables banaction
  copy:
    dest: /etc/fail2ban/jail.d/00-nftables.local
    content: |
      [DEFAULT]
      banaction = nftables-multiport
      banaction_allports = nftables-allports
    mode: "0644"
    backup: true
  register: fail2ban_banaction
  become: true

- name: Restart fail2ban with new banaction
  when: fail2ban_banaction.changed
  systemd:
    name: fail2ban
    state: restarted
  become: true
```

Validation: `fail2ban-client status sshd` then `nft list table inet f2b-table` shows the ban set. `iptables -L | grep f2b-` returns nothing.

Active bans persist across the cutover — fail2ban re-applies them from its database on restart, into the new framework.

Effort: 1h including the `roles/fail2ban` edit and a unban/reban cycle test.

#### Phase 4 ordering

Order doesn't matter — the three are independent. Suggested sequence: netavark first (highest-traffic surface, validates that real workloads tolerate native-nft netavark before we touch the rest), fail2ban second (lowest blast radius — bans are easily reversible), libvirt last (release-gated; may sit indefinitely).

After phase 4 completes, **the iptables-nft compat layer has no users**. We can `apt purge iptables-nftables-compat` (and the `iptables` binary it provides) for a real "no iptables tooling on disk" finish. Probably not worth it — the package is small and human operators may want `iptables -L` for muscle-memory introspection of *legacy* hosts before they're converted.

## Per-role rule contribution

Today every firewall rule lives centrally in `rules.v4.j2`. If a future service role wants to open a port (a metrics scraper bound to a non-loopback interface, a syncthing replica accepting LAN peers), it has to fork the central template — couples the service role to the firewall role. nftables makes the alternatives meaningfully cheaper. Three patterns, ranked.

### Pattern A — central list var

The simplest answer. The iptables role declares a default-empty list; inventory or other roles set it; the template loops.

```yaml
# roles/iptables/defaults/main.yml
iptables_input_rules_extra: []

# host_vars/some-host.yml
iptables_input_rules_extra:
  - { saddr: "@lan_admin_sources", proto: tcp, dport: 9090, comment: "prometheus" }
```

```jinja
{% for r in iptables_input_rules_extra %}
{% if r.saddr is defined %}ip saddr {{ r.saddr }} {% endif %}{{ r.proto }} dport {{ r.dport }} accept comment "{{ r.comment }}"
{% endfor %}
```

Works on iptables today (with the right syntax flip) — doesn't require the migration. **But**: ansible doesn't merge lists across var sources. A service role can't declare its own port without the operator stitching it into the list at host_vars/group_vars level. Cross-role composition is the caller's problem. Fine for one or two host-specific exceptions; doesn't scale.

### Pattern B — include dir (nft only)

nftables has first-class `include` directive that splices a file's contents at the lexical position. The central `nftables.conf` reserves a slot inside the input chain:

```nft
table ip homelab {
    chain input {
        type filter hook input priority filter; policy drop;

        ct state established,related accept
        # ... standard allows ...

        # Per-role contributions land here, before the catch-all reject.
        include "/etc/nftables.d/input/*.nft"

        limit rate 5/minute burst 10 packets log prefix "[nftables] INPUT:REJECT: "
        # ... catch-all reject ...
    }
}
```

A service role drops its own snippet:

```yaml
# roles/prometheus/tasks/main.yml
- name: Open prometheus scrape port
  copy:
    dest: /etc/nftables.d/input/prometheus.nft
    content: |
      ip saddr @lan_admin_sources tcp dport 9090 accept comment "prometheus scrape"
    mode: "0644"
  register: prometheus_fw_rule
  become: true

- name: Reload nftables for prometheus
  when: prometheus_fw_rule.changed
  command: nft -f /etc/nftables.conf
  become: true
```

Aggregation is filesystem-native — `ls /etc/nftables.d/input/` is the audit point, no ansible var-merge magic. Each role owns its file; removing the role removes the file (state: absent on the rebuild). Atomic apply still holds: `nft -f` re-parses the whole central file with the new include set, transactional in the kernel.

This is awkward on iptables: `iptables-restore` has no native include, so the equivalent is an `assemble`-module-style concatenation of `rules.v4.head` + glob + `rules.v4.tail` before each apply. Workable, materially less clean. **This is the strongest unilateral nft argument** — the rest of the migration is mostly cleanup; this is a capability iptables genuinely lacks.

Risk: a syntactically broken snippet wedges the *entire* ruleset on next apply (transactional semantics work against you). Mitigation in the helper role below.

### Pattern C — helper role wrapping pattern B

`roles/firewall_rule` is the Pattern B mechanics with validation and pre-flight checks. Same shape as `roles/systemd_unit` and `roles/podman_secret`.

```yaml
# roles/firewall_rule/tasks/input.yml
- name: firewall_rule | drop snippet for {{ firewall_rule_name }}
  copy:
    dest: /etc/nftables.d/input/{{ firewall_rule_name }}.nft
    content: "{{ firewall_rule_body | trim }}\n"
    mode: "0644"
    backup: true
  register: firewall_rule_result
  become: true

# Pre-validate the assembled ruleset BEFORE the live apply. A broken
# snippet must not wedge the firewall — restore-and-fail is the policy.
- name: firewall_rule | validate assembled ruleset
  when: firewall_rule_result.changed
  command: nft -c -f /etc/nftables.conf
  changed_when: false
  register: firewall_rule_validate
  failed_when: firewall_rule_validate.rc != 0
  become: true

- name: firewall_rule | apply for {{ firewall_rule_name }}
  when: firewall_rule_result.changed
  command: nft -f /etc/nftables.conf
  changed_when: true
  become: true
```

Caller:

```yaml
- import_role:
    name: firewall_rule
    tasks_from: input
  vars:
    firewall_rule_name: prometheus
    firewall_rule_body: |
      ip saddr @lan_admin_sources tcp dport 9090 accept comment "prometheus scrape"
```

Body is raw nft for flexibility — service roles compose whatever they need (port ranges, multiple ports, set-references, ct-status matches). A constrained-API variant (`firewall_rule_port`, `firewall_rule_proto`, `firewall_rule_source`) would be friendlier but covers fewer cases; pick raw.

Removal:

```yaml
- import_role:
    name: firewall_rule
    tasks_from: input_absent
  vars:
    firewall_rule_name: prometheus
```

Ordering dependency: `roles/iptables` must run before any service role calling `firewall_rule` — the central nftables.conf has to exist for `nft -c -f` to validate. Already true today via site.yml ordering ("iptables runs early in site.yml" per the comment in main.yml).

### Recommendation

Adopt **Pattern C** as part of the nft migration if the migration happens. Don't add it on iptables — the include-less assemble dance isn't worth the helper role. If we don't migrate, **Pattern A** covers the host-specific exception case, and we accept that cross-role composition isn't supported.

Pattern C strengthens the migration's defer/do calculus: the moment a second service role wants to open a port, helper-driven includes pay for themselves; on iptables we'd have to fork the central template each time.

## Risks and rollback

**iptables-nft compat layer collision.** Modern Ubuntu's `iptables` is `iptables-nft` — it writes nftables rules under the names `ip filter` / `ip nat` (the iptables-defaults). Our table is `ip homelab`, so no collision. But a *human* running `iptables -L` on a converted host will see an empty filter table and might assume the firewall is off. Document in a `MOTD` or in the role README.

**Why not just name our tables `ip filter` / `ip nat` so `iptables -L` works?** Tempting — they're just names. But those are the tables iptables-nft writes into for netavark / libvirt / fail2ban (under iptables-nft compat). If we own them, `flush table ip filter` wipes their chains on every apply — back to the wipe-cascade we're trying to escape. Surgical updates (delete-and-re-add only our own chains, by handle) are possible but require tracking rule handles across applies for any additions to shared chains like `INPUT` (where fail2ban inserts jumps). Same bookkeeping iptables-restore does poorly. A separate base chain in `ip filter` at a different priority works for execution but doesn't show in `iptables -L` because iptables-nft only enumerates the chain it created. Conclusion: the trade between "iptables -L works for human eyes" and "atomic per-table apply" is fundamental. Take the latter; document the former. Note that even today, `iptables -L` doesn't show netavark's NETAVARK_FORWARD chain or fail2ban's f2b-* chains without explicit naming — the "iptables -L is the whole firewall" mental model is already wrong.

**netavark firewall driver.** Stays on iptables-nft compat by default (configured nowhere; netavark detects). Lives in its own tables (`ip filter` chain `NETAVARK_FORWARD`, plus DNAT chains in `ip nat`). Independent of our `ip homelab`. **No reload needed when we apply.** A future flip to `firewall_driver = "nftables"` in `containers.conf` would move netavark into native nft (still its own tables) — independent decision.

**libvirt.** Same story — iptables-nft compat, separate tables. `virsh net-destroy / net-start` is no longer needed when our role applies.

**fail2ban.** Has a native nftables action set (`actions.d/nftables-*.conf`). The role currently uses iptables-default; flipping it to nftables is a separate change in `roles/fail2ban`. Until then, fail2ban writes iptables-nft rules in its own tables — independent of our `ip homelab`. No reload needed on apply.

**Boot order.** `iptables-persistent.service` and `nftables.service` both run early (`network-pre.target`). systemctl swap is clean. No race.

**Rollback.** Until phase 3 cleanup, rollback is `apt install iptables-persistent && systemctl disable nftables.service && systemctl enable netfilter-persistent`. After phase 3 it's `apt install iptables-persistent` plus a one-shot `roles/iptables` revert commit. Keep both packages installed during the bake window.

## Open question — defer or do?

**Defer signal:** The user-visible behavior is identical post-migration. The reload tasks we'd delete are working today. The `_verify.yml` counter rewrite is honest churn. If the homelab's pace is "touch this when it breaks", iptables-nft compat works fine and this is an unforced refactor.

**Do signal:** Set-based rules genuinely simplify the LAN+WG admin-source pattern, which is going to grow as we add sites. Killing the reload cascade simplifies the mental model of "what runs after iptables-restore". `nft -c` validation closes a real correctness gap.

Recommendation: **defer until** a third LAN+WG admin service forces the rule pair pattern again, or until we hit a real "iptables-restore wiped X" incident. The migration is well-understood; lock in the design (this doc) and pay it down when there's a forcing function.
