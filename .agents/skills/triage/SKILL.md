---
name: triage
description: Triage a homelab service: resolve prod hosts and units, gather diagnostic state, summarize.
argument-hint: <service-name>
model: sonnet
disable-model-invocation: true
allowed-tools: Bash(ssh lab:*), Bash(ssh pug:*), Bash(ssh fox:*), Bash(ansible-inventory:*), Bash(git log:*), Bash(curl -sS --max-time 5 http://netdata.*), Read, Grep, Glob
---

Triage homelab service **$ARGUMENTS** on prod. Empty input means ask for a
service; whitespace means reject the invocation. Diagnostics are pre-authorized,
but prod mutations still need explicit operator ack.

## Resolve

- Role: require `roles/$ARGUMENTS/`. If missing, report no-such-role plus the
  three nearest `roles/` matches.
- Hosts: find `- $ARGUMENTS` in `site.yml`, take each enclosing play's `hosts:`
  pattern, expand with `ansible-inventory -i hosts.ini --list --limit`, and
  intersect with `[prod]` from `hosts.ini`. Skip `localhost`.
- Units: combine candidates from role `templates/` and `files/`
  `*.{service,timer,socket}{,.j2}`, `systemd_unit_args.src`, and bare
  `systemd:` or `service:` tasks. Strip `.j2` for on-host names.
- Instances: for `foo@.service`, enumerate live instances with
  `systemctl list-units 'foo@*' --no-legend --plain`.
- Containers: for podman units, parse `--name` from `systemctl cat <unit>`.
  Substitute `%i` for instances; do not assume unit name equals container name.

## Gather

Run one batched Bash command and parallelize per host/unit. Prefix each ssh with
`-o ConnectTimeout=5 -o BatchMode=yes` so one dead host does not stall the
report.

For each host/unit, run the remote service commands with `sudo`:

- `sudo systemctl show -p ActiveState,SubState,Result,UnitFileState,ActiveEnterTimestamp,NRestarts --value <unit>`
- `sudo journalctl -u <unit> --since=-24h -p warning -o short-iso --no-pager | tail -40`
- `sudo journalctl -u <unit> --since=-24h -o short-iso --no-pager | grep -E 'Main process exited|repeated too quickly|segfault|oom-kill|Failed with result|Killed process' | tail -10`
- Podman: `sudo podman inspect <ct> --format '{{json .State}}'`; if unhealthy,
  run `sudo podman healthcheck run <ct>` and capture exit 0/1/125. If there is
  no healthcheck, report `Healthcheck: n/a`.
- Netdata: `curl -sS --max-time 5 'http://netdata.<h>.fahm.fr/api/v1/alarms?active=true'`;
  filter to `systemd_service*` for the unit.

Once locally:

- `git log --since=30.days.ago --format='%h %cr %s' -- roles/$ARGUMENTS/ host_vars/<h>.yml group_vars/all/`
- Mark unpushed when `git log origin/master..HEAD -- roles/$ARGUMENTS/` is
  non-empty.

Skip non-applicable checks cleanly.

## Report

Start with one TL;DR sentence: healthy, degraded, or broken, plus the most
load-bearing evidence.

Then report each host/unit in order, omitting empty sections:

1. State: active/failed/activating, start age, restart count, disabled/masked.
2. Healthcheck: passing/failing/n/a and last failure reason.
3. Recent errors: top lines or grouped phrases.
4. Netdata alerts touching the unit.
5. Recent role changes, last five commits, with unpushed marker.

Keep the normal report around 80 lines. When noisy, collapse errors/alerts to
counts and top phrases so the operator can choose the next drill-down.
