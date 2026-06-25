#!/usr/bin/env python3
# [MISE] description="Sync HA GUI YAML files (automations/scripts/scenes) between the ha_gui_config clone and lab. Default mode: pull then push."
# [USAGE] arg "<mode>" help="pull | push | sync (default)" default="sync"
# [USAGE] flag "--dry-run" help="preview a push (diff + syntax validation) without writing to the host, moving the synced tag, or reloading HA"
"""
Bidirectional sync for Home Assistant GUI YAML.

The gitignored ha_gui_config clone is the source of truth for these files.
`last_synced_to_host` records the clone commit whose tree matches lab, so push
can refuse when the host has changed since the last pull.

pull: copy host files into the clean clone, commit changes, advance the tag.
push: commit local clone edits, validate changed files, upload to lab, advance
the tag, then reload domains or restart HA for files without a hot reload.
push --dry-run: compare the host to the working tree, validate and print what
would change, but do not write to the host or move git refs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Resolve mise's op:// HA_API_TOKEN before calling the HA API.
if os.environ.get("HA_API_TOKEN", "").startswith("op://") and not os.environ.get("_HA_SYNC_OP_RESOLVED"):
    _op = shutil.which("op")
    if _op:
        _env = {**os.environ, "_HA_SYNC_OP_RESOLVED": "1"}
        sys.exit(subprocess.run([_op, "run", "--", sys.executable, __file__, *sys.argv[1:]], env=_env).returncode)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLONE = REPO_ROOT / "roles/homeassistant/files/ha_gui_config"
HOST = "lab"
HOST_DIR = "/mnt/services/homeassistant"
HA_URL = "https://homeassistant.lab.fahm.fr"
SYNCED_TAG = "last_synced_to_host"


# reload_service sentinel: the file lands on disk and HA picks it up with no
# service call — YAML-mode Lovelace dashboards are re-read (mtime-cached) on the
# next dashboard load, so a browser refresh shows the change.
NO_RELOAD = "__no_reload__"


@dataclass(frozen=True)
class SyncFile:
    rel: str
    mode: str
    reload_service: str | None
    pull: bool


@dataclass(frozen=True)
class PendingChange:
    file: SyncFile
    host_bytes: bytes | None


# Spec tuple: (glob, mode, reload_service, pull).
# `None` reload means a changed file needs homeassistant.restart; one restart
# covers every changed file, otherwise the touched domains reload individually.
# NO_RELOAD means the file just needs to be on disk (no service call).
# `pull=False` makes a file push-only (repo -> host, never captured back): used
# for repo-owned artifacts the HA UI can't edit, so a pull must not clobber the
# clone copy with the host's stub.
# Mode is 0644 to match the homeassistant role's "Ensure files exist" stub —
# otherwise push (0600) and converge (0644) fight over the bits every run. None
# of these GUI YAML files carry secrets (those stay in the un-synced
# secrets.yaml), so group/other read is harmless.
SYNC_SPEC = [
    ("automations.yaml", "0644", "automation.reload", True),
    ("scripts.yaml", "0644", "script.reload", True),
    ("scenes.yaml", "0644", "scene.reload", True),
    ("templates.yaml", "0644", "template.reload", True),
    ("input_numbers.yaml", "0644", "input_number.reload", True),
    ("input_selects.yaml", "0644", "input_select.reload", True),
    ("timers.yaml", "0644", "timer.reload", True),
    # The `counter` integration registers no reload service (only increment/
    # decrement/reset/set_value), so a new or changed counter only loads on
    # restart — None triggers homeassistant.restart for the whole file.
    ("counters.yaml", "0644", None, True),
    # `statistics` sensors have no hot reload, so restart for the whole file.
    ("sensors.yaml", "0644", None, True),
    # climate_template is a legacy `climate:` platform — no hot reload, restart.
    ("climate.yaml", "0644", None, True),
    # HA returns a clear API warning if this reload service is unavailable.
    ("custom_templates/*", "0644", "homeassistant.reload_custom_templates", True),
    # Reload automations so blueprint consumers re-read changed sources.
    ("blueprints/automation/*", "0644", "automation.reload", True),
    # YAML-mode Lovelace dashboards: repo-owned (read-only in the HA UI), so
    # push-only; HA re-reads them on the next load, so no reload service.
    ("dashboards/*", "0644", NO_RELOAD, False),
]


def enumerate_files(for_pull: bool = False) -> list[SyncFile]:
    return [
        SyncFile(path.relative_to(CLONE).as_posix(), mode, reload_service, pull)
        for glob, mode, reload_service, pull in SYNC_SPEC
        if pull or not for_pull
        for path in sorted(CLONE.glob(glob))
        if path.is_file()
    ]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def host_file(rel: str) -> bytes | None:
    r = subprocess.run(
        ["ssh", HOST, f"sudo cat {HOST_DIR}/{rel} 2>/dev/null"],
        capture_output=True,
        check=False,
    )
    return r.stdout if r.returncode == 0 else None


def reload_plan(changes: list[PendingChange]) -> tuple[list[str], bool]:
    reloads = {change.file.reload_service for change in changes}
    if None in reloads:
        return ["homeassistant.restart"], True
    return sorted(reload for reload in reloads if reload and reload != NO_RELOAD), False


def validate_syntax(relpaths: list[str]) -> None:
    """Parse each .yaml/.jinja file; abort with the full failure list if any won't load.

    YAML uses a multi-constructor that lets HA's custom tags (!input, !secret,
    !include) parse to None -- we only care about syntactic validity here, not
    that every tag resolves. Jinja uses Environment().parse() for syntax only.
    """
    import yaml
    from jinja2 import Environment, TemplateSyntaxError

    class _HALoader(yaml.SafeLoader):
        pass

    _HALoader.add_multi_constructor("!", lambda loader, suffix, node: None)

    failures: list[str] = []
    jinja_env = Environment()
    for rel in relpaths:
        path = CLONE / rel
        try:
            if rel.endswith((".yaml", ".yml")):
                with path.open() as f:
                    yaml.load(f, Loader=_HALoader)
            elif rel.endswith(".jinja"):
                jinja_env.parse(path.read_text())
            # other extensions: skip (no parser)
        except (yaml.YAMLError, TemplateSyntaxError) as e:
            failures.append(f"  {rel}: {type(e).__name__}: {e}")
        except OSError as e:
            failures.append(f"  {rel}: cannot read: {e}")
    if failures:
        sys.exit("refusing: syntax errors in ha_gui_config files:\n" + "\n".join(failures))


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def blob_sha_at(ref: str, filename: str) -> str | None:
    """sha256 of file's blob at git ref; None if the file doesn't exist at that ref."""
    r = subprocess.run(
        ["git", "show", f"{ref}:{filename}"],
        cwd=CLONE,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    return sha256(r.stdout)


def worktree_sha(filename: str) -> str | None:
    """sha256 of the file as it sits in the clone working tree; None if absent.

    Used by `push --dry-run`, which doesn't auto-commit, so the comparison point
    is the working tree (uncommitted edits included) rather than the HEAD blob.
    """
    try:
        return sha256((CLONE / filename).read_bytes())
    except OSError:
        return None


def assert_clone_present() -> None:
    if not (CLONE / ".git").exists():
        sys.exit(
            f"ha_gui_config clone not present at {CLONE}. It is an in-place gitignored clone "
            f"(not a submodule); re-clone homelab_ha_config there, and run ha:sync from the main checkout."
        )


def assert_clean_working_tree() -> None:
    r = sh(["git", "status", "--porcelain"], cwd=CLONE)
    if r.stdout.strip():
        sys.exit(f"refusing: clone working tree has uncommitted changes:\n{r.stdout}\ncommit or stash first.")


def commit_and_push(message: str) -> bool:
    """Stage, commit, and push clone changes. Returns True if anything changed."""
    if not sh(["git", "status", "--porcelain"], cwd=CLONE).stdout.strip():
        return False
    sh(["git", "add", "-A"], cwd=CLONE)
    sh(["git", "commit", "-m", message], cwd=CLONE)
    sh(["git", "push", "origin", "main"], cwd=CLONE)
    return True


def has_tag(name: str) -> bool:
    r = sh(["git", "tag", "-l", name], cwd=CLONE, check=False)
    return name in r.stdout.split()


def advance_synced_tag() -> None:
    """Move SYNCED_TAG to HEAD (locally + on origin)."""
    sh(["git", "tag", "-f", SYNCED_TAG], cwd=CLONE)
    sh(["git", "push", "origin", "--force", f"refs/tags/{SYNCED_TAG}"], cwd=CLONE)


def upload_to_host(files: list[SyncFile]) -> None:
    """Upload via /tmp, then sudo install with owner, mode, and backup."""
    pid = os.getpid()
    for file in files:
        safe = file.rel.replace("/", "_")
        tmp_remote = f"/tmp/.ha_sync_{pid}_{safe}"
        sh(["scp", "-q", str(CLONE / file.rel), f"{HOST}:{tmp_remote}"])
        # Make sure the parent dir exists on the host before installing.
        host_parent = f"{HOST_DIR}/{file.rel.rsplit('/', 1)[0]}" if "/" in file.rel else HOST_DIR
        sh(
            [
                "ssh",
                HOST,
                f"sudo install -d -o homeassistant -g homeassistant -m 0755 {host_parent} && sudo install -o homeassistant -g homeassistant -m {file.mode} -b {tmp_remote} {HOST_DIR}/{file.rel} && sudo rm -f {tmp_remote}",
            ]
        )


def _ha_post(service: str) -> None:
    """POST /api/services/<domain>/<action> with the bearer. service is `domain.action`."""
    token = os.environ.get("HA_API_TOKEN", "").strip()
    if not token:
        print(
            f"WARN: HA_API_TOKEN unset; skipping {service}. Files landed on disk; reload/restart manually.",
            file=sys.stderr,
        )
        return
    domain, _, action = service.partition(".")
    url = f"{HA_URL}/api/services/{domain}/{action}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"{service}: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"WARN: {service} failed: HTTP {e.code} {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"WARN: {service} failed: {e.reason}", file=sys.stderr)


def _print_diff_header(label: str) -> None:
    print(f"\n\033[1;34m=== {label} ===\033[0m", flush=True)


def show_push_diff(changed: list[PendingChange]) -> None:
    """For each file about to be pushed, print a colored diff host->HEAD."""
    import tempfile

    for change in changed:
        with tempfile.NamedTemporaryFile(suffix=f"_{Path(change.file.rel).name}") as host_tmp:
            host_tmp.write(change.host_bytes or b"")
            host_tmp.flush()
            status = "new on host" if change.host_bytes is None else "modified on host"
            _print_diff_header(f"push: {change.file.rel} ({status})")
            # --no-index exits 1 when files differ; ignore.
            subprocess.run(
                ["git", "diff", "--color=always", "--no-index", "--", host_tmp.name, str(CLONE / change.file.rel)],
                check=False,
            )


def do_pull() -> None:
    assert_clone_present()
    assert_clean_working_tree()
    sh(["git", "fetch", "--tags", "--force"], cwd=CLONE)
    sh(["git", "pull", "--ff-only", "--quiet"], cwd=CLONE)
    for file in enumerate_files(for_pull=True):
        target = CLONE / file.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            subprocess.run(["ssh", HOST, f"sudo cat {HOST_DIR}/{file.rel}"], stdout=out, check=True)
    if not sh(["git", "status", "--porcelain"], cwd=CLONE).stdout.strip():
        if (
            not has_tag(SYNCED_TAG)
            or sh(["git", "rev-parse", SYNCED_TAG], cwd=CLONE).stdout.strip()
            != sh(["git", "rev-parse", "HEAD"], cwd=CLONE).stdout.strip()
        ):
            advance_synced_tag()
            print("pull: no host changes; advanced tag to HEAD")
        else:
            print("pull: in sync, no-op")
        return
    _print_diff_header("pull: host-side changes about to be committed")
    subprocess.run(["git", "diff", "--color=always", "HEAD"], cwd=CLONE, check=False)
    commit_and_push(f"pull: capture GUI edits from lab ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})")
    advance_synced_tag()
    print(
        f"pull: committed GUI edits + pushed (tag now at {sh(['git', 'rev-parse', '--short', 'HEAD'], cwd=CLONE).stdout.strip()})"
    )


def do_push(dry_run: bool = False) -> None:
    assert_clone_present()
    if not dry_run:
        sh(["git", "fetch", "--tags", "--force"], cwd=CLONE)
        sh(["git", "pull", "--ff-only", "--quiet"], cwd=CLONE)
        # Auto-commit any working-tree edits so `ha:sync push` works straight
        # from a direct file edit in the clone without a manual git commit.
        if commit_and_push(f"push: local edits ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"):
            print("push: committed local edits")
    if not has_tag(SYNCED_TAG):
        sys.exit(f"refusing: no {SYNCED_TAG} tag. Run `mise run ha:pull` once to establish the baseline.")
    # Per-file state model:
    #   tag_blob  -- sha at last_synced_to_host, or None if file is new to sync
    #   head_blob -- sha at clone HEAD (always exists; file is in HEAD by definition)
    #   host_blob -- sha on lab, or None if file isn't deployed yet
    # diverged   = host has stale-but-present content edited away from tag
    #              (would clobber on push without a pull first)
    # changed    = host content differs from HEAD (missing-on-host counts)
    diverged: list[str] = []
    changed: list[PendingChange] = []
    for file in enumerate_files():
        # dry-run compares the working tree (uncommitted edits included); a real
        # push has already folded those into HEAD via commit_and_push.
        head_blob = worktree_sha(file.rel) if dry_run else blob_sha_at("HEAD", file.rel)
        tag_blob = blob_sha_at(SYNCED_TAG, file.rel)
        host_bytes = host_file(file.rel)
        host_blob = sha256(host_bytes) if host_bytes is not None else None
        if tag_blob is not None and host_blob is not None and host_blob != tag_blob:
            diverged.append(file.rel)
            continue
        if host_blob != head_blob:
            changed.append(PendingChange(file, host_bytes))
    if diverged:
        msg = [f"refusing: host diverged from {SYNCED_TAG} (GUI/host edited since last sync):"]
        msg += [f"  {f}" for f in diverged]
        msg.append("\nrun `mise run ha:pull` to capture host-side edits, then retry push.")
        sys.exit("\n".join(msg))
    if not changed:
        print("push: HEAD matches host, nothing to do")
        return
    show_push_diff(changed)
    validate_syntax([change.file.rel for change in changed])
    if dry_run:
        services, restart_required = reload_plan(changed)
        if restart_required:
            reload_desc = "homeassistant.restart (a changed file has no hot reload)"
        else:
            reload_desc = ", ".join(services) or "none"
        print(f"\n\033[1;33mdry-run\033[0m: would upload {[change.file.rel for change in changed]}")
        print(f"\033[1;33mdry-run\033[0m: would advance {SYNCED_TAG} and trigger: {reload_desc}")
        print("\033[1;33mdry-run\033[0m: nothing written to host, no git refs moved, HA not reloaded.")
        return
    upload_to_host([change.file for change in changed])
    advance_synced_tag()
    print(f"push: uploaded {[change.file.rel for change in changed]}")
    services, restart_required = reload_plan(changed)
    if restart_required:
        print("at least one changed file requires a restart -- restarting homeassistant")
    for service in services:
        _ha_post(service)
    # YAML-mode dashboards need no service call; nudge the operator that the
    # change is live but only visible after a browser refresh.
    if any(change.file.reload_service == NO_RELOAD for change in changed):
        print("note: dashboard change is live -- refresh the browser to see it")


def main() -> None:
    # Parse --dry-run from argv (mise forwards args verbatim); also honour the
    # usage-spec env var mise sets for the declared flag, so either path works.
    raw = sys.argv[1:]
    dry_run = "--dry-run" in raw or os.environ.get("usage_dry_run") == "true"
    positional = [a for a in raw if a != "--dry-run"]
    mode = (positional[0] if positional else "sync").strip()
    if dry_run and mode not in ("push", "sync"):
        sys.exit("--dry-run only applies to push")
    if mode == "sync":
        if dry_run:
            # pull mutates (commits host edits); a dry-run previews the push half only.
            print("dry-run: skipping pull; previewing push only")
            do_push(dry_run=True)
            return
        do_pull()
        do_push()
    elif mode == "push":
        do_push(dry_run=dry_run)
    elif mode == "pull":
        do_pull()
    else:
        sys.exit(f"unknown mode {mode!r}; use pull | push | sync")


if __name__ == "__main__":
    main()
