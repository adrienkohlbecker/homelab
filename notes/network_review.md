# Network documentation review

Audit of `Dataroom/Network documentation.xlsx` cross-referenced against the
live repo. Findings grouped by severity. Companion artifact:
[notes/network_topology.yml](network_topology.yml).

## A. Errors in the spreadsheet (resolved)

The spreadsheet had six concrete errors; all are reconciled in
[notes/network_topology.yml](network_topology.yml) and confirmed by the
operator. Summary kept for the audit trail:

1. Wireguard "Fixed IPs" table used `10.123.128.x/129.x/130.x/185.x`;
   real wg lives in `10.123.64.0/18`. **Resolved**: per the global
   convention `wg = physical + 64.0.0`, so lab=`.64.2`, pug=`.64.3`,
   box=`.64.5`, bunk=`.121.3`. Spreadsheet table dropped.
2. `10.123.4.14` listed twice (both Venta AH510s). **Resolved**: Hugo
   moves to `.15`.
3. `/27` math: row 65 said `.32/27` ends at `.65`; it ends at `.63`.
   **Resolved**: YAML uses derived ranges.
4. `lab` on IoT VLAN was `.4.15` in the spreadsheet; reality is `.4.102`.
   **Resolved**: YAML matches reality.
5. "Old firewall forward" port-forward table referenced dead hosts.
   **Resolved**: not migrated to the YAML.
6. `10.123.4.3` double-claimed between `otgw` and Philips AC0850 Félix.
   **Resolved**: otgw device decommissioned; the IP is Philips AC0850
   Félix. The dead otgw *config* is a separate cleanup task — see §C.

## B. Design issues with the scheme itself

7. **Retire `host_virtualization_ranges.libvirt: 10.123.48.0/21` too.**
   By the same logic as the podman retirement (B.8): libvirt's default
   network is NAT'd, VMs aren't cross-host reachable, the per-host /24s
   are host-local. The /21 framing claims a shared parent that's
   decorative — the per-host /24s already live in
   `hosts.<n>.libvirt_bridge` and that's the only place they need to.
   Drop `host_virtualization_ranges` entirely; libvirt joins podman in
   "host-local, recorded in the host registry, no shared parent
   reservation needed."

   Counter-argument: keeping the /21 reservation signals "don't reuse
   these /24s for something else." But the YAML enumerates every
   `libvirt_bridge` already, so any future allocator looking up "is
   `.55.0/24` free" can scan the host registry. The reservation isn't
   load-bearing.

8. **Retire `10.123.40.0/21`** (confirmed). Done in the YAML — the
   block is gone, with a comment explaining the choice. If per-host
   container bridges ever become useful, the right structure is
   `hosts.<n>.podman_bridge: <cidr>` parallel to `libvirt_bridge`, not
   a sibling /21.

9. **VLAN 1 = Management antipattern — deferred.** Worth a migration
   eventually (`id: 1` → `id: 10` on UDM + trunked switch ports + tagged
   clients + the YAML), but not urgent in a single-admin lab.

10. **Better way to do per-host /27 blocks?** The current scheme has
    visible waste; a few alternatives, none clearly winning:

    Current state: each host gets a /27 (32 IPs) inside `10.123.1.0/24`.
    The role's actual carving ([roles/macvlan/tasks/podman.yml:4-7](../roles/macvlan/tasks/podman.yml#L4-L7))
    splits it into lower /28 (host mac0 at `.1` + 14 dead slots
    aspirationally for libvirt-on-macvlan VMs that nobody uses) and
    upper /28 (16 podman containers). Today `macvlan_enable_podman` is
    `false` on every host, so the only macvlan'd containers actually
    deployed live in the *shared* `.1.224/27` keepalived block (pihole)
    and the IoT VLAN's `10.123.4.96/30` (homeassistant). Each host's
    per-host /27 is essentially holding only its mac0 iface at `.1`.

    **Option A — Tighten to /28 per host.** Doubles host capacity in
    `10.123.1.0/24` (8 → 16 hosts). Refactor cost: the macvlan role's
    "/28+/28 split" assumption goes away; instead the whole /28 is the
    ip_range, with host at `.1` and containers at `.2-.15`. Existing
    container IPs renumber — `lab .1.16-.31` becomes `lab .1.2-.15`,
    pug from `.48-.63` to `.18-.31`, etc. Disruptive vs. the payoff
    (you free 50% of a /24 you weren't going to use anyway).

    **Option B — Drop per-host blocks; use the management VLAN as a
    flat pool.** Allocate macvlan'd container IPs on demand from
    `10.123.1.0/24` (with each host's mac0 iface at the low end and
    keepalived VIPs at the high end). Track them in the YAML's
    `static_leases` rather than in a structural block. Pro: less
    pre-allocation, easier to see what's actually deployed. Con: loses
    "which host owns this IP" at-a-glance from the IP alone (you'd
    consult the registry instead).

    **Option C — Status quo.** Accept the 2× over-allocation in a /16
    that's <1% used. The waste is invisible in practice and any
    refactor renumbers live containers. Default unless one of A/B has
    a specific motivator.

    My read: **C** until something changes. The wasted /28 per host
    only matters when `10.123.1.0/24` fills up, which won't happen at
    current scale. If the next change is "add a 9th host", **A** with
    the renumbering is right. If the next change is "more diverse
    macvlan'd services with no obvious per-host owner", **B** is right.

11. **Replicate per-host blocks across every home VLAN (adopted).**
    Goal: any host can put a VM or container into any VLAN without
    inventing an ad-hoc carve-out (the way homeassistant currently
    grabs `10.123.4.96/30`). Encoded in the YAML; details below.

    **L2 constraint.** A macvlan/macvtap child of `eth0.<vlan>` lives
    on that VLAN's L2 segment. Its IP *must* come from the VLAN's own
    /24 — a "parallel virtual /24" on another subnet won't reach the
    same ARP domain. So each VLAN's per-host blocks have to be carved
    inside its existing `10.123.X.0/24`, not in some shared `10.123.Y.0`
    range. That changes the sizing math.

    **Adopted layout (per /24 VLAN).** Eight host slots indexed 0–7,
    parallel between the static_fixed primary range and the
    macvlan_blocks range:

    ```
    .0           network
    .1           gateway
    .2   – .9    host primaries  (8 slots: .2=lab .3=pug .4=box
                                  .5=pom .6-.9 reserved). Holds the
                                  host's eth0.<vlan> static IP when
                                  the host trunks this VLAN.
    .10  – .63   static fixed devices  (54 slots, /26 minus primaries)
    .64  – .127  DHCP pool             (64 IPs, two /27s)
    .128 – .143  slot 0  →  lab macvlan_block   /28
    .144 – .159  slot 1  →  pug                  /28
    .160 – .175  slot 2  →  box                  /28
    .176 – .191  slot 3  →  pom                  /28
    .192 – .207  slot 4  reserved                /28
    .208 – .223  slot 5  reserved                /28
    .224 – .239  slot 6  reserved                /28
    .240 – .255  slot 7  reserved                /28
    ```

    DHCP shrinks to 64 IPs per VLAN. Still ample for IoT/Home
    (current load well under 50). Slot ordering mirrors the existing
    Management /27 blocks (lab→pug→box→pom).

    **Two IPs per host per VLAN — primary + mac0, mandatory.** Each
    host trunking a given VLAN has:

    1. **`eth0.<vlan>` primary** — lives in the static_fixed host
       slot (e.g. lab's IoT primary at `10.123.4.2`). Inbound
       management/ssh; host-originated outbound.

    2. **`mac0` macvlan child of `eth0.<vlan>`** — lives at the
       first IP of the host's /28 macvlan_block (e.g. lab's IoT
       mac0 at `10.123.4.128`). Required because the Linux kernel
       forbids a macvlan parent from communicating directly with
       its own children (parent↔own-child packets are dropped, but
       peer children can talk fine). mac0 is a peer-child of
       eth0.<vlan> and bypasses the quirk, so the host can always
       reach its own VMs/containers on this VLAN.

    Each /28 block carves as:
    ```
    .128         mac0
    .129 – .135  lower /29 minus mac0 — libvirt macvtap VMs (7 slots)
    .136 – .143  upper /29 — podman macvlan containers (8 slots, the
                              role's ip_range)
    ```

    **Macvlan role tweak.** The existing role uses
    `macvlan_host_ip = subnet | ipmath(1)`, which lands mac0 at the
    *second* IP of the block — a no-cost convention when blocks were
    `/27` (32 IPs) but a real-cost waste on `/28` (only 16 slots).
    Switch to `ipmath(0)` so mac0 occupies slot 0 (`.128`) of each
    block; reclaims one IP per block (32 IPs across the estate).
    No L2 semantics force the gap — the block is a logical partition
    of the parent /24, not a routed subnet.

    **What "libvirt macvlan" means here.** Today libvirt VMs sit on
    a NAT'd per-host bridge (`10.123.48.0/24` for lab) and reach the
    LAN through the host's IP. To put a VM *on a VLAN* you need
    libvirt's macvtap (or Linux-bridge) mode — each VM's domain XML
    declares an interface attached to `eth0.<vlan>` and gets an IP
    from the host's /28 in that VLAN. That's a per-VM config change;
    the NAT'd `libvirt_bridge` keeps existing for VMs that don't need
    L2-on-VLAN presence (test runners, etc.).

    **Switch-side cost.** Today each host's switch port is access on
    VLAN 1 + tagged VLAN 4 on lab only. Hosting things on additional
    VLANs means turning the relevant host port into a trunk carrying
    VLANs 2/3/4 too — UDM-Pro reconfig per port. Not hard, but real,
    and only required for VLANs where a given host actually hosts
    something.

    **Migration (IoT, the one VLAN with deployed consumers today).**
    Bounded change, lab-side only:
    - lab's `eth0.4` primary: `10.123.4.102` → `10.123.4.2` (host
      slot 0 in `static_fixed`).
    - lab's `mac0` on IoT: **new** at `10.123.4.128`.
    - homeassistant `ip_range`: `10.123.4.96/30` → `10.123.4.136/29`
      (upper /29 of lab's IoT /28; 8-slot pool, no pin).
    - 14 IoT static device leases shift up by 8 (`.2–.15` → `.10–.23`)
      to clear `.2–.9` for host primaries; update dnsmasq
      reservations and pihole config accordingly.

    Home and Work blocks are net-new — nothing to migrate.

## C. Improvements layered on top of the YAML artifact

15. **Replace the static lists in `group_vars/prod.yml` with derivations
    from `notes/network_topology.yml`.** Today the same IPs are
    duplicated across `external_ips`, `site_subnets`, `wireguard_peers`,
    plus the spreadsheet. Wire the YAML in as `vars_files:` and let
    everything downstream consume it. See *Consumption snippets* below.

16. ~~Pi-hole static leases from YAML~~ — out of scope (operator's call;
    leases stay UI/dnsmasq-managed).

17. ~~Drop the spreadsheet from the dataroom~~ — done.

18. ~~Decommission the dead `otgw` config.~~ **Done.**
    - [roles/nginx/tasks/main.yml](../roles/nginx/tasks/main.yml)
      now carries a `state: absent` tombstone using the new toggle
      in [roles/nginx/tasks/site.yml](../roles/nginx/tasks/site.yml);
      removes `/etc/nginx/sites-{available,enabled}/otgw` on next
      apply and reloads.
    - `external_ips.otgw` + `otgw_admin_password` dropped from
      [group_vars/prod.yml](../group_vars/prod.yml) and
      [group_vars/test.yml](../group_vars/test.yml).

## D. Consumption snippets

### Ansible

Make the YAML a first-class data source under `group_vars/`. One file
per group, all loading the same canonical YAML:

```yaml
# group_vars/all/network.yml
network: "{{ lookup('file', playbook_dir ~ '/notes/network_topology.yml') | from_yaml }}"
```

Then everywhere that currently hard-codes a 10.123.x literal:

```yaml
# was: home_subnet: 10.123.0.0/16
home_subnet: "{{ network.supernet }}"

# was: site_subnets: { home: 10.123.0.0/21, brumath: ..., bonniers: ... }
site_subnets: "{{ network.sites | dict2items
                  | rejectattr('key', 'match', '^reserved_')
                  | items2dict(key_name='key', value_name='value')
                  | map('combine') }}"  # simpler: just `network.sites.home.cidr` etc.

# was: external_ips.lab: 10.123.0.2
ansible_host: "{{ network.hosts[inventory_hostname].physical }}"

# wireguard_peers becomes a derivation, not a literal:
wireguard_peers: "{{
  (network.wireguard.servers  | dict2items | map('combine', {'is_server': true})) +
  (network.wireguard.clients  | dict2items | map('combine', {'is_server': false}))
}}"
```

For per-host vars (`host_vars/lab.yml`):

```yaml
libvirt_default_network: "{{ network.hosts.lab.libvirt_bridge }}"
macvlan_subnet:          "{{ network.hosts.lab.macvlan_block }}"
```

### Terraform / OpenTofu

```hcl
locals {
  network = yamldecode(file("${path.module}/../notes/network_topology.yml"))
}

# Example: Cloudflare DNS A-record driven by host registry
resource "cloudflare_record" "lab" {
  zone_id = var.zone_id
  name    = "lab"
  type    = "A"
  value   = local.network.hosts.lab.physical
}

# Example: iterating servers
resource "cloudflare_record" "wg" {
  for_each = local.network.wireguard.servers
  zone_id  = var.zone_id
  name     = "wg-${each.key}"
  type     = "A"
  value    = each.value.address
}
```

Two things to check before wiring this in: (a) ansible loads
`group_vars/` *before* the playbook's CWD is fully resolved, so the
`playbook_dir`-relative path needs verification on a real run; (b)
terraform's `file()` happens at plan time, so the YAML becomes a state
dependency — any edit triggers a plan. Both are normal; just call them
out.

## E. Open questions

All resolved as of this revision. New questions land here as they
appear; today there are none.
