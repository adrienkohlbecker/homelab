---
description: Triage a homelab service — resolve host(s), gather state, summarize
argument-hint: <service-name>
model: sonnet
disable-model-invocation: true
allowed-tools: Bash(ssh lab:*), Bash(ssh pug:*), Bash(ssh fox:*), Bash(ansible-inventory:*), Bash(git log:*), Bash(curl -sS --max-time 5 http://netdata.*), Read, Grep, Glob
---

Triage the homelab service **$ARGUMENTS** on prod.

If `$ARGUMENTS` is empty, ask which service. If it contains whitespace, reject — one service per invocation.

## Step 1 — Resolve

If `roles/$ARGUMENTS/` doesn't exist, stop and report no-such-role plus the 3 nearest matches (`ls roles/ | grep -i <fragment>`).

**Host(s).** Grep `site.yml` for `- $ARGUMENTS` (leading hyphen-space to avoid substring matches); a role may appear in multiple plays. For each enclosing play's `hosts:` pattern, expand via `ansible-inventory -i hosts.ini --list --limit '<pattern>'` and intersect with the `[prod]` group from `hosts.ini`. Skip `localhost` (operator's workstation).

**Unit(s).** Gather candidates from all three sources — they don't fully overlap:

- `roles/$ARGUMENTS/{templates,files}/*.{service,timer,socket}{,.j2}` (strip `.j2` for the on-disk name).
- `grep -rh systemd_unit_args.src roles/$ARGUMENTS/tasks/` — canonical when the role uses the helper (CLAUDE.md § Helper Roles).
- Bare `systemd:`/`service:` task names — covers package-provided units like `nut-server.service` (no template, hyphen not underscore).

For instanced units (`foo@.service`), enumerate live instances per host: `ssh <h> "systemctl list-units 'foo@*' --no-legend --plain"`, triage each.

**Container name** (podman units): parse `--name <value>` from the unit's `ExecStart` via `ssh <h> "systemctl cat <unit>"` — don't assume unit-name == container-name. For `@.service`, substitute `%i` from the instance.

## Step 2 — Gather

Issue all commands in parallel via a single batched Bash call. Every ssh prefixed with `-o ConnectTimeout=5 -o BatchMode=yes` so one unreachable host doesn't stall the report. Connection multiplexing comes from `~/.ssh/config`.

Per `(host, unit)`:

- `ssh <h> "sudo systemctl show -p ActiveState,SubState,Result,UnitFileState,ActiveEnterTimestamp,NRestarts --value <unit>"` — machine-parsable state; `UnitFileState` surfaces `disabled`/`masked`.
- `ssh <h> "sudo journalctl -u <unit> --since=-24h -p warning -o short-iso --no-pager | tail -40"` — recent warning+ entries with ISO timestamps.
- `ssh <h> "sudo journalctl -u <unit> --since=-24h -o short-iso --no-pager | grep -E 'Main process exited|repeated too quickly|segfault|oom-kill|Failed with result|Killed process' | tail -10"` — systemd-frame failures `-p warning` misses.
- Podman only: `ssh <h> "sudo podman inspect <ct> --format '{{json .State}}'"` — Health + Restarts + StartedAt + ExitCode in one shot. If `.Config.Healthcheck.Test` is empty/`[]`, report `Healthcheck: n/a` rather than printing the empty struct. If `.State.Health.Status` is `unhealthy`, actively re-probe: `ssh <h> "sudo podman healthcheck run <ct>"` (exit 0/1/125).
- `curl -sS --max-time 5 'http://netdata.<h>.fahm.fr/api/v1/alarms?active=true'` — active netdata alarms; filter to `systemd_service*` for this unit. Best-effort; if netdata is unreachable, note and continue.

Once per service (local repo):

- `git log --since=30.days.ago --format='%h %cr %s' -- roles/$ARGUMENTS/ host_vars/<h>.yml group_vars/all/` — commits within blast radius. Mark "unpushed" if `git log origin/master..HEAD -- roles/$ARGUMENTS/` is non-empty.

Skip non-applicable commands gracefully (podman inspect on a native daemon, healthcheck run on a no-health container).

## Step 3 — Report

**TL;DR first** (1 sentence): broken / healthy / degraded, plus the single most load-bearing observation (which journal line, which commit, which alert).

Then per `(host, unit)`, in order, skip empty sections:

1. **`<host>:<unit>`** — header
2. **State** — active / failed / activating, started <relative> ago, restart count if non-zero. Surface `disabled`/`masked` here as the first finding.
3. **Healthcheck** — passing / failing / n/a, last failure reason if unhealthy.
4. **Recent errors** — top 10 deduped lines with ISO timestamps.
5. **netdata alerts** — active alerts touching this unit, if any.
6. **Recent role changes** — last 5 commits in the 30-day window with relative dates; mark unpushed.

Budget: ~15 lines per pair, capped at ~80 lines total. When over budget, collapse sections 4–6 to counts ("23 warnings in last 24h, top phrases: …") and let the operator drill down by re-running.
