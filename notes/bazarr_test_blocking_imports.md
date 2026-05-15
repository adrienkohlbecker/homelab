# Why bazarr's QEMU boot needs `use_sonarr: false` / `use_radarr: false`

## Symptom

Under `test/testrole.py bazarr`, the unit takes ~3 minutes from `systemctl start bazarr` to "Started bazarr" notification. On lab (prod), the same unit boots in 15–19s. CPU is idle the whole time — 8s of CPU consumed over 240s wall-clock. That ruled out emulation / interpreter overhead and pointed at a blocking I/O wait.

## Root cause

`bazarr/utilities/analytics.py:85` constructs an `EventTracker()` instance **at module-import time**, unconditionally — `settings.analytics.enabled` is gated only inside the per-event `track_*` methods, never around construction. The constructor populates user-property metadata for Google Analytics 4:

```python
class EventTracker:
    def __init__(self):
        ...
        self.sonarr_version = get_sonarr_info.version()   # HTTPS GET to sonarr
        self.radarr_version = get_radarr_info.version()   # HTTPS GET to radarr
```

Each `version()` call does `requests.get(..., timeout=int(settings.sonarr.http_timeout))` (60s in our config) against `https://{sonarr|radarr}.{inventory_hostname}.{domain}/api/system/status`.

In the box-qemu VM:
- `sonarr.box.fahm.fr` / `radarr.box.fahm.fr` resolve via the wildcard `*.fahm.fr` → `10.123.0.5` (lab WAN). DNS succeeds.
- TCP SYN to `10.123.0.5:443` from inside the test VM never gets an answer — slirp/the test path doesn't route there.
- Each `requests.get` blocks the full 60s budget, returns `'unknown'`.

So:
- 60s sonarr `version()` + 60s radarr `version()` = **120s** to import `utilities.analytics`.
- Bazarr's signalr client warms up after that and re-uses the same machinery → another **~60s**.
- Total: ~3 minutes of pure TCP timeout waits.

Bisected with an `__import__` hook inside the running container; `utilities.analytics` was the only import over 0.5s, at 120.20s.

## Why an upstream upgrade doesn't help

`bazarr/utilities/analytics.py` was last touched **2023-10-17** — file is byte-identical on `master`, latest `v1.5.7-beta.x`, and our pinned `v1.5.2`. No open PR addresses it (searched for `analytics`, `EventTracker`, `lazy`, `startup`, `slow`, `defer`).

A related commit `39661d2` (Feb 2026, in `v1.5.7-beta.*`) actually makes this *worse*: it stops caching `'unknown'` as a valid result for `get_{sonarr,radarr}_info.version()`. With that change, the cache no longer absorbs repeat calls on an unreachable upstream, so every re-call re-blocks 60s instead of returning the cached failure. So a naive bump from 1.5.2 → 1.5.7-beta would degrade behaviour here.

Issue #1506 (the parent of #1519) reported a closely-related symptom in 2021 and was "fixed" in v0.9.8-beta.3 — but the fix only wrapped failures in try/except so bazarr doesn't crash. The 60s-per-call timeout remained, and the analytics tracker grafted the same blocking calls onto module-import time when it was added in 2023.

## What our role does

[roles/bazarr/templates/config.yaml.j2](../roles/bazarr/templates/config.yaml.j2) gates two settings on `qemu_test`:

1. `use_sonarr` / `use_radarr` → `false` in test, `true` in prod.

   `get_sonarr_info.version()` early-returns when `settings.general.use_sonarr` is false (same for radarr). With this, `EventTracker()` constructor still runs but the version calls are no-ops, so import time drops from ~120s to <1s.

2. `sonarr.http_timeout` / `radarr.http_timeout` → `5` everywhere.

   Defence-in-depth so any future re-enabling of `use_*` (e.g. an integration variant, or a transient sonarr outage in prod) caps the worst-case block at ~10s instead of ~120s. Bazarr defaults to 60s; for a same-LAN sonarr/radarr that's unnecessarily lenient — if either is unreachable in 5s the request was going to fail anyway, and waiting longer just lengthens bazarr's startup window. Applies in prod too.

## Why not other approaches

| Considered | Rejected because |
|---|---|
| Stand up stub sonarr/radarr in `_setup.yml` | Large complexity for the integration value gained; can't cheaply fake the v3 `/api/system/status` JSON response shape across bazarr's branching. |
| Make DNS not resolve for `*.box.fahm.fr` | Wildcard is defined in Cloudflare; removing test-host subdomains breaks other roles that legitimately need them to *route* (even if not reach). |
| Lower `TimeoutSec` in the unit | Originally kept at 300s to absorb this bug. Once the config gate fixed cold-start to <1s, that became pure padding; tightened to 120s (and `--stop-timeout` to 60s) -- still 5-6× real boot/stop, with slack for genuinely slow-disk paths. |
| Patch bazarr's `analytics.py` | Carries a vendored-image-mod cost forever and would re-break on every linuxserver/bazarr image bump; the config gate is image-version-independent. |

## Forward signals

- If linuxserver/bazarr ever ships with `EventTracker` lazy-instantiated, the `use_*` gate becomes pure noise and can be removed.
- If sonarr/radarr ever run inside the box-qemu test variant (e.g. an "integration" matrix cell), the gate switches off naturally — they'd be reachable.
