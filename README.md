# homelab

Ansible-driven configuration for my home infrastructure: a handful of bare-metal Ubuntu hosts running ZFS-on-root, podman services behind nginx, WireGuard between sites, and Cloudflare DNS managed via OpenTofu. Everything in this repo is reproducible from a fresh disk: Packer bakes the OS image, Ansible converges per-host configuration, and a Python harness exercises any role end-to-end inside QEMU before it touches a real machine.

`CLAUDE.md` is the canonical reference for conventions (role layout, helper roles, test variants, commit style). This README is the map.

## Repository map

| Path | Contents |
| --- | --- |
| `hosts.ini`, `host_vars/`, `group_vars/` | Inventory and the current host, network, and service configuration |
| `site.yml` | Top-level playbook — base install, services, lab-only roles, reboot check |
| `wireguard.yml` | Renders one client config on demand for `mise run wg:show <device>` (localhost; streamed to a QR or stdout, never written to disk) |
| `bunk.yml` | One-shot config for the off-site `bunk` peer |
| `roles/` | Role directories for services, system concerns, and shared helpers |
| `packer/` | QEMU fixture and server-image builds |
| `terraform/` | DNS, cloud resources, and supporting infrastructure |
| `test/`, `unit_tests/` | End-to-end QEMU harness and unit tests |
| `mise-tasks/`, `mise.toml` | Tool pinning, environment, and task entrypoints |
| `notes/` | Long-form design notes referenced from code comments (private clone, gitignored) |
| `vault-client.sh`, `ansible.cfg` | Vault identity lookup and Ansible runtime configuration |

## Common workflows

```sh
mise trust && mise install
mise run ansible --limit lab --tags nginx --check
mise run tf plan
mise run packer:build box
mise run test:role -- nginx
mise run test
mise run lint
```

Run `mise tasks` for the complete task catalog. See [CLAUDE.md](CLAUDE.md) for conventions, command variants, testing details, and operational safeguards.

## Secrets

Ansible uses two vault ids: `prod` is workstation-only, while `test` is also available to CI. `vault-client.sh` resolves their passwords; see [Vault ids](CLAUDE.md#vault-ids-prod-vs-test) for storage and lookup details. Other external credentials remain `op://` references in `mise.toml`, and WireGuard client configs are rendered on demand with `mise run wg:show`.

## License

MIT — see `LICENSE`.
