# homelab

Ansible-driven configuration for my home infrastructure: a handful of bare-metal Ubuntu hosts running ZFS-on-root, podman services behind nginx, WireGuard between sites, and Cloudflare DNS managed via OpenTofu. Everything in this repo is reproducible from a fresh disk: Packer bakes the OS image, Ansible converges per-host configuration, and a Python harness exercises any role end-to-end inside QEMU before it touches a real machine.

`CLAUDE.md` is the canonical reference for conventions (role layout, helper roles, test variants, commit style). This README is the map.

## Hosts

`hosts.ini` defines the managed hosts, network devices, and test VMs:

| Host        | Role                                                        | Disk shape          |
| ----------- | ----------------------------------------------------------- | ------------------- |
| `lab`       | Main server: DNS, media, Gitea archive, MinIO, libvirt, home-automation | mdadm-EFI + 3-disk mirror rpool + dozer/tank(raidz2)/mouse |
| `pug`       | Secondary server (DNS mirror via keepalived, zfs autobackup target) | rpool + 2 apoc      |
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
| `roles/` | Role directories for services, system concerns, and shared helpers |
| `group_vars/`, `host_vars/` | Inventory variables (vault values inline as `!vault`) |
| `packer/` | `qemu.pkr.hcl` builds the `box` / `pug` / `lab` QEMU images and their fixture pools |
| `terraform/` | Cloudflare DNS + Nexus repos; OpenTofu state encrypted in MinIO |
| `test/` | asyncio harness — `testrole.py` (one role on one VM), `testall.py` (matrix) |
| `mise-tasks/`, `mise.toml` | Tool pinning, env (1Password refs), `lint` / `fmt` / `tf` / `packer:build` tasks |
| `zbm/`, `zbm-build/` | ZFSBootMenu image build (docker buildx + upstream Dockerfile) and aarch64 scaffolding |
| `notes/` | Long-form design notes referenced from code comments (private clone, gitignored) |
| `vault-client.sh` | Resolves the ansible-vault password per vault-id (`prod`/`test`) from env var, macOS keychain, or `~/.config/homelab/vault-pass-<id>` |
| `ansible.cfg` | Wires `hosts.ini` + `vault-client.sh`; enables mitogen strategy and persistent SSH |

## Roles

Roles map 1:1 to a service or system concern, and their order in `site.yml` is the dependency order. Helper-role contracts and per-role conventions (artifact URL+sha colocation, test hooks `_setup.yml` / `_verify.yml`, `qemu_test` gating, the check-mode-user idiom, and the `service_ports:` registry) are documented in **CLAUDE.md**.

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
mise run test:role -- kuma                        # defaults to --machine box
mise run test:role -- zfs --machine lab --keep    # on-demand prod-shape regression
mise run test:all -- --jobs 5            # full role × machine matrix

# Lint / format
mise run lint
mise run fmt

# Secrets in ansible variable
ansible-vault encrypt_string
```

## Test harness

The QEMU harness lives in `test/`; use `testrole.py` for one role and `testall.py` for the matrix. See [Testing Guidelines](CLAUDE.md#testing-guidelines) for its lifecycle, fixtures, exit codes, and artifacts.

## Secrets

Ansible uses two vault ids: `prod` is workstation-only, while `test` is also available to CI. `vault-client.sh` resolves their passwords; see [Vault ids](CLAUDE.md#vault-ids-prod-vs-test) for storage and lookup details. Other external credentials remain `op://` references in `mise.toml`, and WireGuard client configs are rendered on demand with `mise run wg:show`.

## License

MIT — see `LICENSE`.
