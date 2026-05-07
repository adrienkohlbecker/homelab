# Seed ISO replacement plan

## Summary

`test/utils.py:build_seed_iso()` shells out to `cloud-localds` (Linux only) or
falls back to `xorrisofs` / `mkisofs` / `genisoimage` to produce a
`cidata`-labelled ISO9660 seed for the `minimal` cloud-image variant. On macOS
this forces a `brew install xorriso` step — recorded in `/Users/ak/Work/homelab/AGENTS.md` line 46 — that exists solely to bootstrap one VM variant. The seed is consumed at `/Users/ak/Work/homelab/test/machine.py:741` and attached as a virtio raw drive at line 753.

NoCloud is still happy with an ISO9660 image labelled `cidata`, so the plan is to keep the on-the-wire shape (an ISO at `<workdir>/seed.img`, attached the exact same way) and replace the subprocess with **`pycdlib`** — a pure-Python ISO9660 writer on PyPI, BSD-3, no compiled deps. `build_seed_iso` and its single call site in `machine.py` stay byte-for-byte equivalent to the operator; the only diff is `pyproject.toml` grows one dep and `AGENTS.md` loses the `xorriso` line.

## Transport survey

| Transport | What it is | Host-side build tool | cloud-init support | Mac availability | Code complexity | Verdict |
|-----------|-----------|----------------------|--------------------|------------------|-----------------|---------|
| **ISO9660 cidata** | ISO9660 (joliet+rock-ridge) volume labelled `cidata`, `user-data` + `meta-data` at root, attached as virtio drive | `genisoimage` / `xorrisofs` / `cloud-localds` *or* `pycdlib` (pure Python) | Original NoCloud transport, supported in every cloud-init since 0.7.x. Active on jammy 24.x and noble 24.x. | `pycdlib` from PyPI; `xorriso` from brew (today's state) | Low (~25 LoC of pycdlib calls) | **Recommended**: pure-Python, zero host deps |
| **vfat cidata** | FAT12/16/32 filesystem labelled `cidata`, same file layout | `mkfs.vfat` + `mtools` (`mcopy`) | Documented alongside ISO9660 since the early NoCloud datasource ("vfat or iso9660 filesystem, with the filesystem volume label being `cidata` or `CIDATA`") | `brew install dosfstools mtools` — works on arm64 but adds two formulas | Low–medium: `truncate` + `mkfs.vfat` + `mcopy` | Rejected: heavier than pycdlib for the same outcome |
| **kernel cmdline `ds=nocloud;s=…`** | `-append "... ds=nocloud;s=file:///path/"`. cloud-init reads `user-data`/`meta-data` from that local dir inside the guest. | None — qemu `-append` only | Supported but only useful in `-kernel`/`-append` boots. The minimal variant boots Ubuntu's cloud image via SeaBIOS/UEFI off vda; we don't `-kernel` it. Adding `-kernel` would require extracting kernel+initrd from the cloud qcow2 first — exactly the dependency we're trying to avoid | n/a | Would need kernel/initrd extraction first | Rejected: shifts the dependency |
| **SMBIOS serial `ds=nocloud;s=…`** | qemu `-smbios type=1,serial=ds=nocloud;s=…`. cloud-init reads it from DMI and treats it like the kernel cmdline. | None — qemu args only | Supported. Same `s=` URL grammar applies. | n/a | Lowest *if* it could carry payload | Rejected: NoCloud's `s=` is a `seedfrom=<URL>`, **not** inline payload. With `s=file:///…` the path is dereferenced *inside the guest* (which doesn't have our host files). The only useful URL is `http://10.0.2.2:<port>/` → degenerates into "NoCloud-net". Plus SMBIOS Type 1 `serial` is the system-serial-number field (mis-used per upstream issue #3133), single null-terminated string with practical caps well below `user-data`'s ~700 B today — so even an inline-via-serial design wouldn't be future-proof. |
| **NoCloud-net (HTTP)** | qemu SMBIOS or kernel cmdline `s=http://10.0.2.2:N/`, host runs an HTTP server. | Python `http.server` | Supported on every modern cloud-init (jammy/noble accept both `ds=nocloud-net` and `ds=nocloud;s=http://…`). | n/a | Higher: spin up an `http.server` task in `prepare()`, race it with boot, tear down in `stop()`. Picks up a port, plumbs `10.0.2.2` (qemu SLIRP gateway) into the SMBIOS string. | Rejected: more code, not less |
| **fw_cfg (`opt/com.coreos/config`)** | `-fw_cfg name=opt/com.coreos/config,file=…` | None | **Not supported** by NoCloud / cloud-init. fw_cfg is the **Ignition** (CoreOS / FCOS) transport; cloud-init has no fw_cfg reader. The user's note conflated the two. | n/a | n/a | Rejected: doesn't exist for cloud-init |

### Cloud-init versions on the targets

- jammy `jammy-updates` ships cloud-init 24.4.1 (SRU'd to 25.x).
- noble `noble-updates` ships the same 24.x/25.x line.

Both are well past every transport listed; ISO9660-cidata has been stable since 0.7.x (~2014). Version gating is academic.

## Recommendation

**Replace the subprocess fallback chain with `pycdlib`.** Output stays an ISO9660 image labelled `cidata` containing `user-data` + `meta-data` at root, identical to what `cloud-localds` and `xorriso` produce today. `Machine.prepare()`'s call site is unchanged. No qemu argument changes, no cloud-init config drift between Linux and Mac.

Why pycdlib over alternatives:

- **Pure Python.** Already on PyPI, BSD-3, no native build step. Fits beside `passlib` / `dnspython` in `pyproject.toml`; `uv sync` picks it up.
- **No host-side tooling drift.** `cloud-localds` (Ubuntu's `cloud-image-utils`) and `xorriso` (brew) both go away.
- **Compatibility.** pycdlib upstream issue #7 documents the exact arguments that produce a working cidata ISO (`interchange_level=4`, `joliet=True`, `rock_ridge='1.09'`, `vol_ident='cidata'`); recipe is widely cargo-culted against real cloud-init. Mirrors the layout `genisoimage -joliet -rock` produces today.

Why not vfat: needs `dosfstools` + `mtools` on Mac (two new brew formulas vs. zero) for a purely cosmetic difference.

Why not SMBIOS-inline: NoCloud's `s=` grammar is `seedfrom=<URL>`, not raw user-data — config-by-reference only, and the references are URLs cloud-init dereferences from inside the guest. The only useful URL on minimal's user-mode SLIRP network is `http://10.0.2.2:<port>/` backed by a host webserver — strictly more code than packing an ISO.

## Concrete code change

### `/Users/ak/Work/homelab/test/utils.py` — replace `build_seed_iso`

Signature stays the same (so `machine.py` doesn't move). Drops the `shutil.which` chain and subprocess; gains a thin pycdlib wrapper. Async preserved (the function is awaited from `prepare()`); the body is sync I/O wrapped in `asyncio.to_thread(...)` so the loop stays responsive on the ~50 ms ISO write.

```python
import asyncio
from io import BytesIO
from pathlib import Path

import pycdlib


async def build_seed_iso(out: Path, user_data: Path, meta_data: Path) -> None:
    """Pack a NoCloud cidata seed iso for a cloud-init guest.

    Pure-Python writer (pycdlib) so the host doesn't need cloud-localds /
    xorriso / genisoimage. Output is an ISO9660 volume labelled `cidata`
    with `user-data` and `meta-data` at the root, in the joliet+rock-ridge
    shape NoCloud has accepted since 0.7.x.
    """
    await asyncio.to_thread(_write_seed_iso, out, user_data, meta_data)


def _write_seed_iso(out: Path, user_data: Path, meta_data: Path) -> None:
    iso = pycdlib.PyCdlib()
    # interchange_level=4 lifts the 8.3 filename limit so the joliet /
    # rock-ridge names ("user-data", "meta-data") aren't constrained by the
    # ISO9660-1988 baseline. joliet=True + rock_ridge="1.09" matches what
    # `genisoimage -joliet -rock` emitted before; cloud-init's fs probe
    # picks up either extension.
    iso.new(interchange_level=4, joliet=True, rock_ridge="1.09", vol_ident="cidata")
    for src, iso9660_path, joliet_path, rr_name in (
        (user_data, "/USERDATA.;1", "/user-data", "user-data"),
        (meta_data, "/METADATA.;1", "/meta-data", "meta-data"),
    ):
        payload = src.read_bytes()
        iso.add_fp(
            BytesIO(payload),
            len(payload),
            iso9660_path,
            joliet_path=joliet_path,
            rr_name=rr_name,
        )
    iso.write(str(out))
    iso.close()
```

### `/Users/ak/Work/homelab/test/machine.py` — no diff

The minimal-branch lines (around 739-754) stay byte-for-byte. Drive attached as `file=…/seed.img,if=virtio,format=raw` — qemu auto-detects ISO9660 on a raw virtio block device just fine; cloud-init mounts the partition by label, not by qemu-side media type. No `-cdrom`, no `media=cdrom`, no ordering change.

### `/Users/ak/Work/homelab/pyproject.toml`

Add `pycdlib` to `dependencies` next to the other helper deps. Pinning to a major is sufficient; the API we use (`new`, `add_fp`, `write`, `close`) hasn't changed since 1.x:

```toml
"pycdlib>=1.14.0",
```

### `/Users/ak/Work/homelab/AGENTS.md`

Delete the brew clause from line 46. The current paragraph reads:

> The `minimal` variant lazily fetches the upstream Ubuntu cloud image into `<imagedir>/cloud-images/`; **macOS hosts also need `xorriso` (`brew install xorriso`) for the cloud-init seed iso when `cloud-localds` isn't present.** Profiles correspond to host roles…

The bolded clause is removed. No replacement line — pycdlib is pulled in by `uv sync`, so the operator's existing `mise install` / `uv sync` flow already covers it.

## Validation plan

1. **Unit-level smoke** (new file `test/unit/test_seed_iso.py`, ~30 lines, runs in pytest per `pyproject.toml`'s `[tool.pytest.ini_options]`):
   - Call `_write_seed_iso` against the real `test/minimal/{user-data,meta-data}` into a tempfile.
   - Reopen with `pycdlib.PyCdlib(); iso.open(out)` and assert:
     - `iso.pvd.volume_identifier.rstrip(b"\x00 ").decode() == "cidata"`
     - `/user-data` and `/meta-data` resolve via `iso.get_record(rr_path=…)`
     - both files round-trip byte-for-byte against the source.

2. **End-to-end on Mac (arm64)** with `xorriso` *uninstalled* (`brew uninstall xorriso`):
   ```
   uv run test/launch.py --machine minimal --ubuntu noble
   ```
   - PID file appears under the workdir → qemu launched with the seed.
   - SSH banner reached within `SSH_WAIT_TIMEOUT` (120 s) → user-data's authorized_keys was applied.
   - SSH in, `sudo cat /run/cloud-init/status.json` shows `done` everywhere.
   - `sudo grep -i nocloud /var/log/cloud-init.log | head` shows `Datasource DataSourceNoCloud` and matched the `cidata` volume label (the canary that we matched the labelled volume rather than fell through to `NoCloudNet`).

3. **End-to-end on Linux (x86_64)**: same command on a dev box. Verifies the path that previously preferred `cloud-localds` still works (we don't call it anymore).

4. **Regression sweep**: `mise run test:all` on both arches; only `minimal` exercises this code, but `box`/`lab`/`pug` should be unchanged.

5. **Cross-check the ISO image**: on a Linux host, `isoinfo -d -i seed.img` should print `Volume id: cidata`; `isoinfo -R -l -i seed.img` should list `user-data` and `meta-data` at root with non-zero sizes.

## Estimated LOC delta

- `test/utils.py`: -26 / +25 (subprocess branch out, pycdlib boilerplate in). Net ~0.
- `test/machine.py`: 0.
- `pyproject.toml`: +1.
- `AGENTS.md`: -1 sentence.
- `test/unit/test_seed_iso.py` (new): +30.

**Total: roughly +30 lines** (entirely the new pytest), but **two host-side dependencies eliminated** (`cloud-localds` on Linux dev boxes, `xorriso`/`xorrisofs` on Mac) and the OS-conditional `shutil.which` chain deleted from the harness. The `cloud-image-utils` Ubuntu package is no longer relevant to the test path either.

## Sources

- [NoCloud datasource (current cloud-init docs)](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
- [pycdlib examples (joliet, rock-ridge)](https://clalancette.github.io/pycdlib/examples.html)
- [pycdlib issue #7 (cidata recipe)](https://github.com/clalancette/pycdlib/issues/7)
- [cloud-init issue #3133 — NoCloud SMBIOS Type 1 serial misuse](https://github.com/canonical/cloud-init/issues/3133)
- [QEMU fw_cfg device spec (Ignition transport, not NoCloud)](https://qemu-project.gitlab.io/qemu/specs/fw_cfg.html)
- [Ignition supported platforms (qemu = fw_cfg)](https://coreos.github.io/ignition/supported-platforms/)
