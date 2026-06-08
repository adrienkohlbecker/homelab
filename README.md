# homelab

Ansible-driven configuration for my home infrastructure: a handful of bare-metal Ubuntu hosts running ZFS-on-root, podman services behind nginx, WireGuard between sites, and Cloudflare DNS managed via OpenTofu. Everything in this repo is reproducible from a fresh disk: Packer bakes the OS image, Ansible converges per-host configuration, and a Python harness exercises any role end-to-end inside QEMU before it touches a real machine.

`CLAUDE.md` is the canonical reference for conventions (role layout, helper roles, test variants, commit style). This README is the map.

## Hosts

`hosts.ini` — five managed machines, a router, and two test VMs:

| Host        | Role                                                        | Disk shape          |
| ----------- | ----------------------------------------------------------- | ------------------- |
| `lab`       | Main server: pihole, media, gitea, minio, libvirt, home-automation  | mdadm-EFI + 3-disk mirror rpool + dozer/tank(raidz2)/mouse |
| `pug`       | Secondary server (pihole mirror via keepalived, zfs autobackup target) | rpool + 2 apoc      |
| `fox`       | External VPS (headscale mesh hub)                           | n/a                 |
| `bunk`      | Off-site Synology NAS (configured via `bunk.yml`)           | n/a                 |
| `udm`       | UDM Pro router (DNS failover via keepalived)                | n/a                 |
| `localhost` | Self-target for wireguard config generation                 | n/a                 |
| `box`       | Test-only QEMU VM (default CI fixture, single-disk ZFS)    | Single-disk rpool   |
| `minimal`   | Test-only QEMU VM (ext4 cloud image, stranger-baseline)    | Vanilla ext4        |

`group_vars/prod.yml` and `group_vars/test.yml` carry the two parallel networks (10.123.0.0/16 prod, 10.234.0.0/16 test); `group_vars/all/main.yml` holds shared knobs (service ports, mirror URLs, ssh keys).

## Layout

| Path | Contents |
| --- | --- |
| `site.yml` | Top-level playbook — base install, services, lab-only roles, reboot check |
| `wireguard.yml` | Renders one client config on demand for `mise run wg:show <device>` (localhost; streamed to a QR or stdout, never written to disk) |
| `bunk.yml` | One-shot config for the off-site `bunk` peer |
| `roles/` | ~120 roles — see "Roles" below |
| `group_vars/`, `host_vars/` | Inventory variables (vault values inline as `!vault`) |
| `packer/` | `qemu.pkr.hcl` builds the `box` / `pug` / `lab` QEMU images; pug/lab pools are baked in by `pools.sh` |
| `terraform/` | Cloudflare DNS + Nexus repos; OpenTofu state encrypted in MinIO |
| `test/` | asyncio harness — `testrole.py` (one role on one VM), `testall.py` (matrix) |
| `mise-tasks/`, `mise.toml` | Tool pinning, env (1Password refs), `lint` / `fmt` / `tf` / `packer:build` tasks |
| `zbm/`, `zbm-build/` | ZFSBootMenu image build (docker buildx + upstream Dockerfile) and aarch64 scaffolding |
| `wireguard/` | Peer keys live (vaulted) in `group_vars`; client configs are rendered on demand (`wg:show`), not stored here |
| `notes/` | Long-form design notes referenced from code comments (private submodule) |
| `vault-client.sh` | Resolves the ansible-vault password per vault-id (`prod`/`test`) from env var, macOS keychain, or `~/.config/homelab/vault-pass-<id>` |
| `ansible.cfg` | Wires `hosts.ini` + `vault-client.sh`; enables mitogen strategy and persistent SSH |

## Roles

Roles map 1:1 to a service or a system concern; the order in `site.yml` reflects boot/dependency order.

- **Bootstrap & OS**: `apt`, `apt_source`, `apt_upgrade`, `bash`, `user`, `cleanup`, `hostname`, `locale`, `console`, `timezone`, `subid`, `hwe_kernel`, `cron`, `logrotate`, `journald`, `unattended_upgrades`
- **Networking**: `netplan`, `wireguard`, `resolved`, `firewall`, `fail2ban`, `ssh` / `ssh_root`, `macvlan`, `avahi`, `postfix`, `ntp`
- **Hardware**: `fan2go`, `hdparm`, `hd_idle`, `smart`, `lm_sensors`, `powertop`
- **Storage / boot**: `zfs`, `zfs_autobackup`, `zfs_mount`, `swap`, `boot`, `zfsbootmenu`, `refind`, `kdump`
- **Container / web stack**: `podman`, `samba`, `certbot`, `nginx`, `authelia`, `homepage`, `services`
- **Monitoring & infra**: `eaton_ipp`, `netdata`, `fluentbit`, `wolweb`, `keepalived`, `custom_exporter`, `nut_server`, `dnscrypt_proxy`, `pihole`, `docker_client`, `socket_proxy`
- **VPN / mesh**: `headscale`, `tailscale`, `udm_dns_failover`
- **Lab-only services**: `libvirt`, `minio`, `influxdb`, `scratch`, `data`, `media`, `jellyfin`, `sonarr` / `radarr` / `bazarr` / `lidarr` / `plex` / `tautulli` / `seerr`, `sort_ini`, `sabnzbd`, `transmission`, `profilarr`, `gitea` (+ `gitea_runner`, `github_runner`, `nodejs`), `getmail`, `mutt`, `speedtest`, `filebrowser`, `mosquitto`, `z2m`, `homeassistant`, `kuma`, `spouse`, `hyperdx`, `nexus`
- **Helpers** (imported by other roles, not used directly): `service_user`, `podman_secret`, `systemd_unit`, `systemd_timer`, `usergroup_immediate`, `sqlite_dataset`, `static_curl`, `linger`, `test`

Helper-role contracts and per-role conventions (artifact URL+sha colocation, test hooks `_setup.yml` / `_verify.yml`, `qemu_test` gating, the check-mode-user idiom, the `service_ports:` registry) are documented in **CLAUDE.md**.

## Common workflows

```sh
# One-time setup
mise trust && mise install            # pins tofu, packer, python, uv, shellcheck, etc.
                                       # uv_venv_auto creates .venv and runs `uv sync` on entry
op signin                              # 1Password CLI, for op:// refs in mise.toml [env]

# Apply
mise run ansible --limit lab
mise run ansible --limit lab --tags nginx --check
mise run wg:show phone                 # terminal QR to enroll a device (laptop: mise run wg:show laptop --conf | pbcopy)

# DNS / Nexus repos
mise run tf plan
mise run tf apply

# Image rebuilds (when the base OS or chroot.sh changes)
mise run packer:build               # all three sources in parallel
mise run packer:build box           # one source (push CI's target)
mise run packer:build --ubuntu noble

# Test a single role end-to-end in QEMU
test/testrole.py kuma                        # defaults to --machine box
test/testrole.py zfs --machine lab --keep    # on-demand prod-shape regression
test/testall.py --jobs 5            # full role × machine matrix

# Lint / format
mise run lint
mise run fmt

# Secrets in ansible variable
ansible-vault encrypt_string
```

## Test harness in one paragraph

`test/testrole.py <role>` boots a QEMU VM (`box` by default; `--machine {minimal,box,lab,pug}`), runs the role's `_setup.yml`, applies the role in check-mode, then for real, then a second time to assert idempotence, then runs the role's `_verify.yml` if present. `test/testall.py` fans this out across N workers and writes a TSV joblog. Output goes to `test/out/<machine>.<role>.ansi`. Variants vary only in disk topology — bootloader (ZBM via rEFInd), filesystem (ZFS-on-root for prod-shaped variants), and arch (x86_64 + Linux/KVM, aarch64 + Mac/HVF) are deliberately fixed. See CLAUDE.md → "Test Environment Design" for why.

## Secrets

- Ansible vault: per-id passwords come from `vault-client.sh` (macOS keychain `homelab-vault-<id>`, Linux file `~/.config/homelab/vault-pass-<id>`, or `HOMELAB_VAULT_PASSWORD_<UPPER_ID>` env var for CI). Two ids in use: `prod` (workstation-only) and `test` (also pushed to CI as a GitHub repo secret). Vaulted values live inline in `group_vars/*.yml` and `host_vars/*.yml`. See CLAUDE.md "Vault ids" for details.
- 1Password: `mise.toml [env]` declares `op://Lab/...` refs for Cloudflare, Nexus, MinIO and the OpenTofu state passphrase. `mise run tf` is wrapped in `op run --` so values are only ever in the wrapped process's env.
- WireGuard: peer private keys are vaulted in `group_vars/{prod,test}.yml` and PSKs derive from a vaulted seed (`roles/wireguard/filter_plugins/wireguard_psk.py`); client configs are rendered on demand by `mise run wg:show <device>` (terminal QR for iOS, `--conf` to stdout for macOS) and never written to disk.

## License

MIT — see `LICENSE`.
