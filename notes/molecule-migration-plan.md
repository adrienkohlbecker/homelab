# Molecule migration plan: testrole.py / testall.py to Molecule (delegated driver)

## Summary

Replace the bespoke `test/testrole.py` (single role driver) and `test/testall.py` (parallel fanout) with [Molecule](https://ansible.readthedocs.io/projects/molecule/) using the **delegated** driver. `QemuMachine` from `test/machine.py` keeps owning VM lifecycle; Molecule owns the test phase orchestration (prepare / converge / idempotence / verify).

The migration replaces ~1500 LOC of orchestration logic (testrole.py ~380 + testall.py ~565 + helpers) with ~250-350 LOC of Molecule scenario configuration plus a thin `machine_cli.py` shim around `QemuMachine`. We trade "one bespoke harness" for "Molecule conventions plus a QEMU adapter".

The smallest viable cutover is **one parameterized Molecule scenario** that reads role/machine/ubuntu from environment variables, plus a tiny pytest-xdist wrapper that sets those env vars and invokes `molecule test` as a subprocess. This keeps Molecule's scenario count at one regardless of how the role × machine × ubuntu matrix grows.

## Scope

In:
- Replace the orchestration layer (testrole.py + testall.py + the four static playbooks under `test/playbooks/`).
- Keep `test/machine.py` essentially as-is — only add a tiny CLI entry point (`test/machine_cli.py`) that wraps `QemuMachine` so Molecule's `create.yml` and `destroy.yml` can shell out to it. One small constructor knob (`external_workdir`) added to `Machine` so VM lifetime can outlast a single Python process.
- Keep `test/utils.py`, `test/arch.py`, `test/setup_mitogen.py`, `test/disks/`, `test/minimal/` unchanged.
- Migrate flag semantics: `--checkmode`, `--no-checkmode`, `--idempotence`, `--no-idempotence`, `--keep`, `--timeout`, `--benchmark`, `--upstream-mirrors`, `--machines`, `--ubuntu`, `--jobs`, `--retry-failed`, `--retry-role`.

Out:
- No structural changes to `test/machine.py` beyond the `external_workdir` knob.
- No changes to per-role `tasks/_setup.yml` / `tasks/_verify.yml` / `tasks/_test.yml` shapes.
- `_mirrors.yml` keeps its current shape — invoked from Molecule's `prepare.yml`.

## Proposed layout

### Why one scenario, parameterized

Matrix is roles (~100) × machines (4) × ubuntu (2) = ~800 combinations.

| Option | Pros | Cons |
| --- | --- | --- |
| One scenario per role | Maps to `molecule test --all` directly; nice `molecule list` output | ~100 scenario dirs; matrix axes (machine, ubuntu) live outside Molecule and re-emerge as env vars anyway |
| One scenario per (role, machine) pair | Closer to flag fidelity | ~400 dirs unmanageable; doubles when ubuntu axis is added |
| **One parameterized scenario** | One `molecule/default/` dir; matrix is pure caller responsibility (env vars + xdist params); zero scenario duplication | `molecule list` is uninformative; need pytest harness or shell loop to fan out |

**Recommendation: one parameterized scenario.** Matches our current model exactly — `testrole.py` is also "one driver, parameterized at invocation time" — and keeps the scenario tree from exploding. Fanout logic moves to a ~120-line pytest file that owns the matrix.

### File layout

```
molecule/
  default/
    molecule.yml         # driver = delegated, parametrization via env
    create.yml           # spawns the QEMU VM via QemuMachine CLI
    destroy.yml          # tears it down
    prepare.yml          # _mirrors + per-role _setup.yml
    converge.yml         # imports the role under test
    verify.yml           # per-role _verify.yml
    requirements.yml     # placeholder for collections (no-op for now)
test/
  machine.py             # unchanged module + a 15-LOC external_workdir knob
  machine_cli.py         # NEW (~80 LOC): create/destroy CLI for delegated driver
  utils.py               # unchanged
  arch.py                # unchanged
  setup_mitogen.py       # unchanged
  pytest_harness.py      # NEW (~120 LOC): runs molecule per parametrize, writes out.tsv
  conftest.py            # NEW (~50 LOC): pytest options, parametrize via pytest_generate_tests
test/playbooks/          # DELETED — replaced by molecule/default/*.yml
test/testrole.py         # DELETED
test/testall.py          # DELETED
```

### `molecule/default/molecule.yml`

```yaml
---
# Molecule scenario for the homelab harness. The driver is "delegated" —
# molecule itself doesn't manage instances; it just calls our create/destroy
# playbooks and expects them to populate the inventory. QemuMachine remains
# the actual VM lifecycle owner.
dependency:
  name: galaxy
  enabled: false   # we vendor roles in `roles/`, no galaxy install step

driver:
  name: default   # alias for "delegated" in modern molecule

platforms:
  # Single platform; the actual triple comes from env so we don't fan out
  # via molecule's matrix. Name is just a label visible in `molecule list`.
  - name: homelab-target
    box: "${MOLECULE_MACHINE:-box}"
    ubuntu: "${MOLECULE_UBUNTU:-jammy}"

provisioner:
  name: ansible
  log: true
  config_options:
    defaults:
      # Reuse repo's ansible.cfg conventions verbatim.
      strategy: mitogen_linear
      strategy_plugins: .ansible-mitogen-strategy
      force_color: true
      callback_result_format: yaml
      check_mode_markers: true
      gathering: smart
      fact_caching: jsonfile
      fact_caching_connection: "${MOLECULE_EPHEMERAL_DIRECTORY}/facts"
      fact_caching_timeout: 7200
    ssh_connection:
      ssh_args: "-o ControlMaster=auto -o ControlPersist=600s -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes"
      pipelining: true
  inventory:
    # Reuse the repo's inventory and group_vars / host_vars so qemu_test:true
    # and the Nexus mirror config land identically.
    links:
      hosts: ../../test/inventory.ini
      group_vars: ../../group_vars
      host_vars: ../../host_vars
  env:
    # Picked up by create.yml / destroy.yml / converge.yml. Exported at
    # invocation time by the pytest harness or the operator's shell.
    MOLECULE_ROLE: "${MOLECULE_ROLE}"
    MOLECULE_MACHINE: "${MOLECULE_MACHINE:-box}"
    MOLECULE_UBUNTU: "${MOLECULE_UBUNTU:-jammy}"
    MOLECULE_KEEP: "${MOLECULE_KEEP:-0}"
    MOLECULE_UPSTREAM_MIRRORS: "${MOLECULE_UPSTREAM_MIRRORS:-0}"
    MOLECULE_SKIP_CHECKMODE: "${MOLECULE_SKIP_CHECKMODE:-0}"
    ANSIBLE_CALLBACKS_ENABLED: "${ANSIBLE_CALLBACKS_ENABLED:-}"

verifier:
  name: ansible   # NOT testinfra; we already have _verify.yml playbooks
```

Notes:
- The `default` driver name is Molecule's modern alias for `delegated` (Molecule 6+ ships it built-in; the old `molecule-plugins[delegated]` add-on may be unnecessary). Verify in the spike.
- `MOLECULE_EPHEMERAL_DIRECTORY` is Molecule's per-scenario workdir (`~/.cache/molecule/<project>/<scenario>/`). Pinning the fact cache there wipes it between runs.
- We deliberately do **not** use Molecule's built-in `idempotence: enabled: true` knob — pytest controls it per invocation so `--no-idempotence` works.

### `molecule/default/create.yml`

The delegated pattern: this playbook runs on `localhost`, spawns the backend, and writes a YAML instance_config Molecule reads to learn how to SSH to the target. We shell out to `python -m machine_cli create` which constructs a `QemuMachine`, prepares overlays, boots, waits for SSH, runs disk setup, then prints the connection details.

```yaml
---
- name: Create QEMU VM via QemuMachine
  hosts: localhost
  gather_facts: false
  vars:
    role: "{{ lookup('env', 'MOLECULE_ROLE') }}"
    machine: "{{ lookup('env', 'MOLECULE_MACHINE') | default('box', true) }}"
    ubuntu_name: "{{ lookup('env', 'MOLECULE_UBUNTU') | default('jammy', true) }}"
    keep_vm: "{{ lookup('env', 'MOLECULE_KEEP') | default('0', true) }}"
    upstream_mirrors: "{{ lookup('env', 'MOLECULE_UPSTREAM_MIRRORS') | default('0', true) }}"
    timeout_seconds: "{{ lookup('env', 'MOLECULE_TIMEOUT') | default('1800', true) }}"
  tasks:
    - name: Spawn QEMU and capture instance config
      ansible.builtin.command:
        argv:
          - python
          - -m
          - machine_cli
          - create
          - --role
          - "{{ role }}"
          - --machine
          - "{{ machine }}"
          - --ubuntu
          - "{{ ubuntu_name }}"
          - --timeout
          - "{{ timeout_seconds }}"
          - "{{ '--keep' if keep_vm == '1' else '--no-keep' }}"
          - "{{ '--upstream-mirrors' if upstream_mirrors == '1' else '--no-upstream-mirrors' }}"
          - --instance-config
          - "{{ molecule_ephemeral_directory }}/instance_config.yml"
          - --workdir-keepfile
          - "{{ molecule_ephemeral_directory }}/qemu.workdir"
        chdir: "{{ playbook_dir }}/../.."   # repo root, where roles/ lives
      environment:
        PYTHONPATH: "{{ playbook_dir }}/../../test"
      changed_when: true   # spawning a VM is always a state change

    - name: Read instance_config back into molecule
      ansible.builtin.include_vars:
        file: "{{ molecule_ephemeral_directory }}/instance_config.yml"
        name: instance_conf

    - name: Populate molecule inventory with the live VM
      ansible.builtin.add_host:
        name: "{{ instance_conf.instance }}"
        groups:
          - molecule
          - test                                  # so group_vars/test.yml applies
          - "{{ instance_conf.inventory_host }}"  # box / lab / pug
        ansible_host: "{{ instance_conf.address }}"
        ansible_port: "{{ instance_conf.port }}"
        ansible_user: "{{ instance_conf.user }}"
        ansible_ssh_private_key_file: "{{ instance_conf.identity_file }}"
        ansible_ssh_common_args: >-
          -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=10
          -o ForwardAgent=yes
        # qemu_test:true and the host_vars file are expressed as connection
        # vars rather than -e on argv, so all subsequent molecule phases see
        # them automatically.
        qemu_test: true
        qemu_test_minimal: "{{ instance_conf.qemu_test_minimal }}"
        _role_under_test: "{{ instance_conf.role }}"
```

`instance_config.yml` shape:

```yaml
---
instance: homelab-target
address: 127.0.0.1
port: 12345                      # picked by QemuMachine in prepare()
user: vagrant
identity_file: packer/vagrant.key
inventory_host: box              # for --limit / host_vars resolution
qemu_test_minimal: false
role: act_runner
```

The `--workdir-keepfile` is a small sentinel containing the path to `QemuMachine.workdir`. Destroy reads it to find the qemu pidfile.

### `molecule/default/destroy.yml`

```yaml
---
- name: Destroy QEMU VM
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Read workdir from keepfile
      ansible.builtin.stat:
        path: "{{ molecule_ephemeral_directory }}/qemu.workdir"
      register: keepfile

    - name: Tear down QEMU
      ansible.builtin.command:
        argv:
          - python
          - -m
          - machine_cli
          - destroy
          - --workdir-keepfile
          - "{{ molecule_ephemeral_directory }}/qemu.workdir"
          - --peak-rss-out
          - "{{ molecule_ephemeral_directory }}/peak_rss_kb"
        chdir: "{{ playbook_dir }}/../.."
      environment:
        PYTHONPATH: "{{ playbook_dir }}/../../test"
      when: keepfile.stat.exists
      changed_when: keepfile.stat.exists
```

### `test/machine_cli.py`

```python
"""CLI shim so molecule's create.yml / destroy.yml can drive QemuMachine.

create:  spawns the VM, waits for SSH, runs disk setup, writes instance_config
         and a workdir keepfile. Exits 0 once the VM is ready.

destroy: reads the workdir keepfile, kills qemu via its pidfile (same code
         path as Machine.__aexit__), captures peak_rss_kb to a sentinel file.
"""
import argparse
import asyncio
import shutil
import sys
from pathlib import Path

import yaml

from machine import QemuMachine, QEMU_MACHINE_SPECS, _read_vm_hwm
from utils import terminate_pid


async def cmd_create(args: argparse.Namespace) -> int:
    m = QemuMachine(
        machine=args.machine,
        role=args.role,
        keep_vm=args.keep,
        ubuntu_name=args.ubuntu,
        machine_timeout=args.timeout,
        upstream_mirrors=args.upstream_mirrors,
        # NEW knob: hand QemuMachine a path it owns until destroy runs.
        # mkdtemp under self.imagedir (same parent _workdir_parent picks).
        external_workdir=Path(args.external_workdir) if args.external_workdir else None,
    )
    # NOTE: we do NOT use `async with m:` — destroy runs in a separate
    # process, so the VM has to outlive this Python.
    await m.prepare()
    await m.boot()
    await m.ensure_booted()
    await m.ensure_ssh()
    await m.run_disk_setup()

    Path(args.workdir_keepfile).write_text(m.workdir.name)
    instance_config = {
        "instance": "homelab-target",
        "address": "127.0.0.1",
        "port": m.ssh_port,
        "user": m.ssh_user,
        "identity_file": "packer/vagrant.key",
        "inventory_host": m.inventory_host,
        "qemu_test_minimal": QEMU_MACHINE_SPECS[args.machine].qemu_test_minimal,
        "role": args.role,
    }
    Path(args.instance_config).write_text(yaml.safe_dump(instance_config))
    return 0


async def cmd_destroy(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir_keepfile).read_text().strip()
    pidfile = Path(workdir) / "pid"
    if pidfile.exists():
        pid = int(pidfile.read_text().strip())
        peak = _read_vm_hwm(pid)
        await terminate_pid(pid, grace_seconds=5)
        Path(args.peak_rss_out).write_text(str(peak))
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create")
    p_create.add_argument("--role", required=True)
    p_create.add_argument("--machine", required=True)
    p_create.add_argument("--ubuntu", required=True)
    p_create.add_argument("--timeout", type=int, default=1800)
    p_create.add_argument("--keep", action=argparse.BooleanOptionalAction, default=False)
    p_create.add_argument("--upstream-mirrors", action=argparse.BooleanOptionalAction, default=False)
    p_create.add_argument("--instance-config", required=True)
    p_create.add_argument("--workdir-keepfile", required=True)
    p_create.add_argument("--external-workdir", default=None)
    p_destroy = sub.add_parser("destroy")
    p_destroy.add_argument("--workdir-keepfile", required=True)
    p_destroy.add_argument("--peak-rss-out", required=True)
    args = parser.parse_args()
    coro = cmd_create(args) if args.cmd == "create" else cmd_destroy(args)
    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
```

### `test/machine.py` change (~15 LOC, the only structural change)

Add an `external_workdir` keyword to `Machine.__init__` so the workdir lifetime can outlast a single Python:

```python
@dataclasses.dataclass
class Machine:
    # ... existing fields ...
    external_workdir: Path | None = None  # NEW: bypass TemporaryDirectory

    def __post_init__(self) -> None:
        # ... existing pre-flight ...
        if self.external_workdir is not None:
            self.external_workdir.mkdir(parents=True, exist_ok=True)
            self.workdir = _ExternalWorkdir(self.external_workdir)
        else:
            self.workdir = tempfile.TemporaryDirectory(dir=self._workdir_parent())
        self._preflight()
```

Where `_ExternalWorkdir` is a tiny duck-type:

```python
class _ExternalWorkdir:
    def __init__(self, path: Path) -> None:
        self.name = str(path)
    def cleanup(self) -> None:
        return  # owned by destroy.yml, not us
```

Avoids the `m.workdir._finalizer.detach()` private-API trick.

### `molecule/default/prepare.yml`

```yaml
---
- name: Configure mirrors and apply per-role setup hook
  hosts: molecule
  gather_facts: true
  tasks:
    - name: Configure apt / podman / pip / uv mirrors
      ansible.builtin.import_role:
        name: _test
        tasks_from: mirrors

    - name: Purge snapd on minimal (avoids snapd.service unit-file warning)
      ansible.builtin.apt:
        name: snapd
        state: absent
        purge: true
        autoremove: true
      become: true
      when: qemu_test_minimal | default(false) | bool

    - name: Run per-role pre-converge hook (tasks/_setup.yml) if present
      ansible.builtin.import_role:
        name: "{{ _role_under_test }}"
        tasks_from: _setup
      when: lookup('first_found', dict(files=[
        'roles/' ~ _role_under_test ~ '/tasks/_setup.yml'
      ], skip=true), default='') | length > 0
```

### `molecule/default/converge.yml`

```yaml
---
- name: Converge (check mode dry-run, then real apply)
  hosts: molecule
  gather_facts: true
  tasks:
    - name: Dry-run the role under test (--check)
      ansible.builtin.import_role:
        name: "{{ _role_under_test }}"
      check_mode: true
      when: lookup('env', 'MOLECULE_SKIP_CHECKMODE') | default('0', true) != '1'

    - name: Apply the role under test
      ansible.builtin.import_role:
        name: "{{ _role_under_test }}"
      check_mode: false
```

### `molecule/default/verify.yml`

```yaml
---
- name: Verify
  hosts: molecule
  gather_facts: true
  tasks:
    - name: Run per-role _verify.yml when present
      ansible.builtin.import_role:
        name: "{{ _role_under_test }}"
        tasks_from: _verify
      when: lookup('first_found', dict(files=[
        'roles/' ~ _role_under_test ~ '/tasks/_verify.yml'
      ], skip=true), default='') | length > 0
```

## Lifecycle mapping

| testrole.py phase | Molecule phase | Notes |
| --- | --- | --- |
| `m.prepare()` (overlay disks, pick ports, copy efivars) | `create.yml` (`python -m machine_cli create`) | The "copy group_vars/host_vars/roles into workdir" step disappears: Molecule's inventory `links` make them accessible directly. |
| `m.boot()` + `ensure_booted()` + `ensure_ssh()` + `run_disk_setup()` | `create.yml` | Same CLI invocation. |
| `_mirrors.yml` | `prepare.yml` (1st task) | Imports `_test` role with `tasks_from: mirrors` directly. |
| Snapd purge on minimal | `prepare.yml` (2nd task) | Gated on `qemu_test_minimal`. |
| Per-role `tasks/_setup.yml` | `prepare.yml` (3rd task) | Gated on file existence via `lookup('first_found')`. Same convention as today. |
| Check mode (`--check`) | `converge.yml` first play | Gated on `MOLECULE_SKIP_CHECKMODE`. Implemented as a `check_mode: true` block, NOT `molecule converge --check` (which would skip the real apply). |
| Real apply | `converge.yml` second play | Standard converge. |
| Idempotence rerun | `molecule idempotence` | Built-in. Reruns converge.yml; fails if any task reports `changed`. The check_mode play emits `changed=N` even on no-op runs in some configurations — verify in spike, fall back to a custom second-converge that scans recap output if needed. |
| Per-role `tasks/_verify.yml` | `verify.yml` (verifier: ansible) | Same idiom, gated on file existence. |
| Journal collection on failure | `block / rescue` in `converge.yml` | Today's `m.collect_journal()`: ssh to target, dump `journalctl --no-pager` to `${MOLECULE_EPHEMERAL_DIRECTORY}/journal.log`. ~15 LOC ansible. Pytest harness tails it on failure. |
| `m.stop()` (kill qemu, capture peak_rss_kb) | `destroy.yml` | `python -m machine_cli destroy` writes peak_rss_kb to a sentinel file in the ephemeral dir. |
| `print_ssh_instructions()` on `--keep` | Pre-`destroy.yml` block | Pytest harness reads `MOLECULE_KEEP` and skips destroy, leaving operator to run `molecule destroy -s default` manually. instance_config has the SSH details. |

## Parallelism strategy

**Recommendation: pytest + pytest-xdist invoking `molecule test`.**

Rejected:
- `molecule test --all --parallel`: only works when matrix is multiple scenarios (~800 dirs). No retry semantics.
- `pytest-molecule`: community plugin, last published 2022, doesn't add anything we need on top of plain pytest+xdist invoking molecule as a subprocess.

The harness:

```python
# test/pytest_harness.py
"""
Pytest entry point for the Molecule-based role harness.

Each parametrized test case is one (role, machine, ubuntu) triple; pytest-xdist
fans them out across N workers via `pytest -n N`. Each test sets the
MOLECULE_* env vars and shells out to `molecule test -s default`. Result
parsing produces an out.tsv compatible with the legacy format.
"""
import csv
import fcntl
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = REPO_ROOT / "test/out.tsv"
LOG_FILE_PREV = REPO_ROOT / "test/out.tsv.prev"
JOBLOG_FIELDS = ["Role", "Ubuntu", "Machine", "Runtime", "Exitval", "PeakKB", "Started"]


def test_role(role: str, machine: str, ubuntu: str, harness_opts: dict) -> None:
    """Run molecule test for one (role, machine, ubuntu) triple."""
    ephemeral = REPO_ROOT / f"test/molecule-ephemeral/{role}.{machine}.{ubuntu}"
    env = {
        **os.environ,
        "MOLECULE_ROLE": role,
        "MOLECULE_MACHINE": machine,
        "MOLECULE_UBUNTU": ubuntu,
        "MOLECULE_SKIP_CHECKMODE": "0" if harness_opts["checkmode"] else "1",
        "MOLECULE_UPSTREAM_MIRRORS": "1" if harness_opts["upstream_mirrors"] else "0",
        "MOLECULE_TIMEOUT": str(harness_opts["timeout"]),
        # Each xdist worker needs its own ephemeral dir so the QEMU pidfile,
        # instance_config, and fact cache don't collide across triples.
        "MOLECULE_EPHEMERAL_DIRECTORY": str(ephemeral),
    }
    if harness_opts["benchmark"]:
        env["ANSIBLE_CALLBACKS_ENABLED"] = "profile_tasks"

    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.time()
    if harness_opts["idempotence"]:
        cmd = ["molecule", "test", "-s", "default"]
    else:
        # Skip idempotence by running each phase explicitly.
        cmd = ["bash", "-c",
               "molecule destroy -s default && "
               "molecule create -s default && "
               "molecule prepare -s default && "
               "molecule converge -s default && "
               "molecule verify -s default && "
               "molecule destroy -s default"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    runtime = time.time() - t0
    peak_kb = _read_peak(ephemeral)
    _append_row(role, ubuntu, machine, runtime, proc.returncode, peak_kb, started)
    if proc.returncode != 0:
        pytest.fail(f"molecule test for {role}.{machine}.{ubuntu} exited "
                    f"{proc.returncode}\nSTDOUT:\n{proc.stdout[-4000:]}\n"
                    f"STDERR:\n{proc.stderr[-4000:]}")


def _read_peak(ephemeral: Path) -> int:
    p = ephemeral / "peak_rss_kb"
    try:
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _append_row(role, ubuntu, machine, runtime, exitval, peak_kb, started) -> None:
    """Append/update one row in out.tsv (xdist-safe via flock)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a+", encoding="utf-8", newline="") as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        h.seek(0)
        rows = list(csv.DictReader(h, delimiter="\t")) if h.read(1) else []
        # ... read existing rows, dedupe on (role, machine, ubuntu), write back
        # (full impl mirrors testall._merge_with_prior + _write_joblog).
```

```python
# test/conftest.py
import pytest


def pytest_addoption(parser):
    parser.addoption("--machines", default="box")
    parser.addoption("--ubuntus", default="jammy")
    parser.addoption("--roles", default="")          # empty = all
    parser.addoption("--retry-failed", action="store_true")
    parser.addoption("--retry-role", default="")
    parser.addoption("--no-checkmode", action="store_true")
    parser.addoption("--no-idempotence", action="store_true")
    parser.addoption("--keep", action="store_true")
    parser.addoption("--timeout", type=int, default=1800)
    parser.addoption("--upstream-mirrors", action="store_true")
    parser.addoption("--benchmark", action="store_true")


@pytest.fixture(scope="session")
def harness_opts(request):
    return {
        "checkmode": not request.config.getoption("--no-checkmode"),
        "idempotence": not request.config.getoption("--no-idempotence"),
        "keep": request.config.getoption("--keep"),
        "timeout": request.config.getoption("--timeout"),
        "upstream_mirrors": request.config.getoption("--upstream-mirrors"),
        "benchmark": request.config.getoption("--benchmark"),
    }


def pytest_generate_tests(metafunc):
    """Materialize the (role, machine, ubuntu) matrix from CLI options."""
    if {"role", "machine", "ubuntu"} - set(metafunc.fixturenames):
        return
    cfg = metafunc.config
    machines = [m.strip() for m in cfg.getoption("--machines").split(",") if m.strip()]
    ubuntus = [u.strip() for u in cfg.getoption("--ubuntus").split(",") if u.strip()]
    if cfg.getoption("--retry-failed"):
        triples = _read_failed_from_log()
    else:
        roles = _resolve_roles(cfg)
        triples = [(r, m, u) for r in roles for m in machines for u in ubuntus]
        if rr := cfg.getoption("--retry-role"):
            wanted = {r.strip() for r in rr.split(",") if r.strip()}
            triples = [t for t in triples if t[0] in wanted]
    metafunc.parametrize(
        ("role", "machine", "ubuntu"), triples,
        ids=[f"{r}-{m}-{u}" for r, m, u in triples],
    )
```

### Flag mapping

| testrole/testall flag | Molecule equivalent |
| --- | --- |
| `--machine box` (single role) | `pytest test/pytest_harness.py -k act_runner-box-jammy` or `--roles act_runner --machines box` |
| `--machines box,lab,pug` | `pytest --machines box,lab,pug` |
| `--ubuntu jammy,noble` | `pytest --ubuntus jammy,noble` |
| `--checkmode` / `--no-checkmode` | `--no-checkmode` (default on); routed to `MOLECULE_SKIP_CHECKMODE` env |
| `--idempotence` / `--no-idempotence` | `--no-idempotence`; harness runs explicit phase chain instead of `molecule test` |
| `--keep` | `--keep`; harness invokes `molecule converge` (skip destroy), prints SSH details from `instance_config.yml` |
| `--timeout SECONDS` | `--timeout`; piped into `MOLECULE_TIMEOUT` then `machine_cli create --timeout` |
| `--benchmark` | `--benchmark`; sets `ANSIBLE_CALLBACKS_ENABLED=profile_tasks`. Harness phase timings come from pytest's `--durations=N`. |
| `--upstream-mirrors` | `--upstream-mirrors`; piped into `MOLECULE_UPSTREAM_MIRRORS` |
| `--jobs N` | `pytest -n N` (xdist) |
| `--retry-failed` | `--retry-failed`; reads `out.tsv` via the same csv idiom |
| `--retry-role X,Y` | `--retry-role X,Y` |
| `--list` | `pytest --collect-only -q` |

## Retry semantics

`testall.py`'s `--retry-failed` and `--retry-role` are bespoke (~30 LOC each). Molecule has no native equivalent. The proposed harness keeps the same `out.tsv` schema (read-modify-write under flock):

- `--retry-failed` reads rows where `Exitval != 0`, parametrizes pytest with exactly those triples, runs them, merges results back into `out.tsv`.
- `--retry-role` filters the freshly-generated matrix by role name.

The xdist-safe write path (flock + read-merge-write) is the only nontrivial new code; testall's serial writer didn't need it.

## Peak RSS

Today: testrole prints `PEAK_KB=12345` to stdout; testall greps it.

Proposed:
- `python -m machine_cli destroy` writes `peak_rss_kb` to `${MOLECULE_EPHEMERAL_DIRECTORY}/peak_rss_kb` as a single integer.
- Pytest harness reads that file after `molecule test` returns and writes it into the `out.tsv` row.
- Cleaner than the stdout-sentinel: no string parsing, no risk of clobbering a future feature.

`_read_vm_hwm()` stays in `machine.py`; the destroy CLI calls it before sending SIGTERM, exactly like `Machine.stop()` does today.

## Bootstrap

Add to `pyproject.toml`:

```toml
dependencies = [
    # ...existing...
    "molecule>=24.0",            # current line as of 2025
    "molecule-plugins>=23.5",    # only if `default` driver isn't bundled — verify in spike
    "pytest-xdist>=3.6",
    "pyyaml",                    # already pulled by ansible, but make explicit for machine_cli
]
```

No `mise.toml` change strictly needed: `python` and `uv` are already pinned, `uv sync` will pull these in. `op://` env injection keeps working since pytest inherits the mise-managed environment. Optional convenience tasks:

```toml
[tasks."test:role"]
description = "Run a single role test (replaces test/testrole.py)"
run = 'pytest test/pytest_harness.py --roles "$1"'

[tasks."test:all"]
description = "Run the full role × machine × ubuntu matrix"
run = 'pytest -n ${MOLECULE_JOBS:-5} test/pytest_harness.py "$@"'
```

`ansible-lint` will start picking up `molecule/default/*.yml`. Either add `exclude_paths: [molecule/]` to `.ansible-lint` or write the playbooks to ansible-lint standards from the start (likely fine; they're tiny).

## Migration phases

### Phase 0 — spike (1 day, throwaway branch, no commits)

Verify the load-bearing assumptions:

1. Does `molecule[ansible]` 24.x ship `default` (delegated) driver out of the box, or do we need `molecule-plugins[delegated]`?
2. Does `molecule idempotence` count `check_mode: true` plays as `changed`? If yes, swap converge.yml shape (run check mode in a separate `molecule converge --` invocation) or skip `molecule idempotence` and reimplement it as a second converge that scans output for `changed=N` (mirrors `testrole._verify_idempotence`).
3. Does `verifier: ansible` with a free-form `verify.yml` match our `_verify.yml` shape?
4. Does `MOLECULE_EPHEMERAL_DIRECTORY` survive across `molecule create` → `molecule converge` → `molecule destroy` invocations?
5. Does `m.workdir._finalizer.detach()` actually keep the temp dir alive across process exit, or do we need the `_ExternalWorkdir` refactor? (The plan assumes we do the refactor — it's 15 LOC and avoids a private-API dependency.)

### Phase 1 — add Molecule alongside legacy harness (~1 day)

1. Add `molecule/default/*.yml`.
2. Add `test/machine_cli.py`.
3. Add `external_workdir` knob to `test/machine.py` (~15 LOC).
4. Add `test/pytest_harness.py` + `test/conftest.py`.
5. Update `pyproject.toml` deps.
6. Smallest cutover: `bash` (35s runtime, no `_setup.yml`, no `_verify.yml`, no extra disks). Run `pytest test/pytest_harness.py --roles bash` end-to-end on Mac.
7. Compare `out.tsv` row to `test/testrole.py bash`. Commit when both report identical exit codes and similar runtimes.

### Phase 2 — port representative role variants (~1 day)

Run the new harness against:
- `bash` — degenerate case (no hooks).
- `act_runner` — has a `tasks/_setup.yml`.
- `healthchecks` — exercises `_verify.yml`, `service_user`, `podman_secret`, `systemd_unit` (most of the helper-role surface).
- `zfsbootmenu` on `--machine minimal` — exercises `qemu_test_minimal` and the cloud-image branch.
- `data` on `--machine lab` — exercises multi-disk and `disk_setup_script`.

Resolve issues that come up (likely candidates: ansible-lint warnings on the new playbooks, fact cache contention between xdist workers, mitogen interaction).

### Phase 3 — full matrix run (~half day, mostly waiting)

Run `pytest -n 5 test/pytest_harness.py --machines box --ubuntus jammy` (equivalent to `testall.py`'s default scope). Compare `out.tsv` row-for-row to a `testall.py` run from the same commit. Investigate divergences.

### Phase 4 — flip mise tasks + AGENTS.md (~half day)

Add `test:role` / `test:all` tasks. Update `AGENTS.md`'s "Testing Guidelines" to point at the new commands. Keep `test/testrole.py` and `test/testall.py` as-is for two weeks as a fallback.

### Phase 5 — delete the legacy harness

Once Phase 3 has been green for 2 weeks:

```
rm test/testrole.py
rm test/testall.py
rm test/playbooks/site.yml          # replaced by molecule/default/converge.yml
rm test/playbooks/_setup.yml        # replaced by molecule/default/prepare.yml
rm test/playbooks/_verify.yml       # replaced by molecule/default/verify.yml
rm test/playbooks/_mirrors.yml      # imported directly in molecule/default/prepare.yml
rmdir test/playbooks
```

`test/inventory.ini`, `test/utils.py`, `test/arch.py`, `test/setup_mitogen.py`, `test/disks/`, `test/minimal/`, `test/machine.py` all stay.

## Risks & unknowns

1. **`molecule idempotence` semantics with `check_mode: true` upstream.** If the dry-run play taints idempotence's "did anything change?" count, rework converge.yml — either move check-mode into its own pytest case (run `molecule converge -- --check` then `molecule converge`), or skip `molecule idempotence` and reimplement it as a second `molecule converge` that scans output for `changed=N` like `testrole._verify_idempotence` does today. **Spike before committing.**

2. **Workdir lifetime across processes.** `tempfile.TemporaryDirectory` was a great fit when one Python owned the entire test; with create and destroy in separate processes we either steal the finalizer (private API) or refactor `Machine` to accept an external path. Plan picks the refactor — `external_workdir: Path | None = None` + `_ExternalWorkdir` shim. ~15 LOC; the only `machine.py` change.

3. **xdist worker collisions.** QemuMachine pre-binds host ports in `prepare()`, so two qemus from two xdist workers won't pick the same SSH forwarding port. VNC display walking (5900..5999) likewise binds before launch. Fact cache lives in `MOLECULE_EPHEMERAL_DIRECTORY` which is per-triple. The only contention point is `out.tsv` writes, handled by flock. Verify in Phase 3.

4. **Journal-on-failure capture.** Today `testrole.py` runs `m.collect_journal()` in the failure branch. Molecule has no native equivalent. Add a `block / rescue` to converge.yml that runs `journalctl --no-pager` over SSH on failure and writes it to `${MOLECULE_EPHEMERAL_DIRECTORY}/journal.log`. ~15 LOC ansible. Pytest harness tails it on failure for parity with `testrole.print_file_tail`.

5. **`molecule destroy` after a failed `molecule create`.** Default Molecule behavior runs destroy on failure (good, matches today), but the workdir keepfile may not exist if create failed early. destroy.yml's `when: keepfile.stat.exists` gate handles that.

6. **mitogen + delegated driver.** Molecule's provisioner sometimes monkey-patches strategy plugins; verify mitogen still loads via `strategy_plugins` config. Easy fallback: drop mitogen for tests and eat the ~12s/run penalty.

7. **macOS HVF parity.** Nothing in the proposal is Linux-specific — `machine_cli.py` is just `QemuMachine` under a different driver — but verify on Mac before Phase 4. Specifically the aarch64 direct-boot path, since that's where `QemuMachine.prepare` does the most work.

8. **AGENTS.md exit-code fidelity.** Today's "Testing Guidelines" documents 0/1/124/125/130. With Molecule, exit codes from `molecule test` are coarser (0 / non-zero); pytest is also binary. We synthesize the granular code by reading `out.tsv` rows in a final pytest hook, or by parsing the molecule subprocess return code + stderr. Document the shift in AGENTS.md as part of Phase 4. Not a blocker.

9. **Test ordering effects under xdist.** xdist by default distributes by test order (loadfile / loadgroup). Two roles starting on the same QEMU host can stress disk and network simultaneously; today's testall semaphore limits to N concurrent. xdist's `-n N` enforces the same upper bound per-process; we're fine.

10. **Two stale unit-test trees.** `test/unit/` and `test/test/` exist; pyproject's `testpaths = ["test/unit"]` confines pytest collection. The new harness lives in `test/pytest_harness.py` (NOT under `testpaths`) so collection scopes don't collide. Run with explicit path: `pytest test/pytest_harness.py`. Verify in spike.

## Estimated LOC delta

| Component | Today (LOC) | After (LOC) | Delta |
| --- | --- | --- | --- |
| `test/testrole.py` | 380 | 0 | -380 |
| `test/testall.py` | 565 | 0 | -565 |
| `test/playbooks/{site,_setup,_verify,_mirrors}.yml` | 24 | 0 | -24 |
| `molecule/default/molecule.yml` | 0 | ~50 | +50 |
| `molecule/default/{create,destroy,prepare,converge,verify}.yml` | 0 | ~120 | +120 |
| `test/machine_cli.py` | 0 | ~80 | +80 |
| `test/pytest_harness.py` | 0 | ~120 | +120 |
| `test/conftest.py` | 0 | ~50 | +50 |
| `test/machine.py` (`external_workdir` knob + `_ExternalWorkdir` shim) | (unchanged) | +15 | +15 |
| **Net** | **969** | **~435** | **-534** |

Numbers are upper bounds — Molecule scenario YAML can shrink once we know which `provisioner.config_options` defaults are already correct.

## Smallest first cutover

```
pytest test/pytest_harness.py --roles bash --machines box --ubuntus jammy
```

The `bash` role has no `_setup.yml`, no `_verify.yml`, no extra disks, no podman secrets, no service users. If the new harness produces an `out.tsv` row with `Exitval=0` and a comparable runtime to `test/testrole.py bash`, the entire pipeline (create → prepare → converge → idempotence → verify → destroy) is wired correctly. Everything beyond that is exercising existing role logic, not new harness code.
