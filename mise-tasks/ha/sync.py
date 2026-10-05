#!/usr/bin/env python3
# [MISE] description="Sync HA GUI YAML files (automations/scripts/scenes) between the ha_gui_config clone and lab. Default mode: pull then push."
# [USAGE] arg "<mode>" help="pull | push | sync (default)" default="sync"
# [USAGE] flag "--dry-run" help="preview a push (diff + syntax validation) without writing to the host, moving the synced tag, or reloading HA"
# [USAGE] flag "--rebase" help="when the clone cannot fast-forward from origin, replay its local commits on top instead of refusing"
"""
Bidirectional sync for Home Assistant GUI YAML.

The gitignored ha_gui_config clone is the source of truth for these files.
`last_synced_to_host` records the clone commit applied on lab, so push can
refuse host-side edits and retry a reload that failed after upload.

pull: commit host files on top of origin, advance the tag to that capture,
then rebase unpushed local commits onto it.
push: commit local clone edits, validate changed files, upload to lab, reload
domains or restart HA for files without a hot reload, then advance the tag.
push --dry-run: compare the host to the working tree, validate and print what
would change, but do not write to the host or move git refs.

Both pull and push bring the clone up to date with origin first, fast-forward
only, so that `last_synced_to_host` stays unambiguous about what is running on
lab. `--rebase` is the escape hatch for a clone that has diverged.
"""

import enum
import getpass
import glob
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
CLONE = REPO_ROOT / "roles/homeassistant/files/ha_gui_config"
HOST = "lab"
HOST_DIR = "/mnt/services/homeassistant"
HA_URL = "https://homeassistant.lab.fahm.fr"
SYNCED_TAG = "last_synced_to_host"
KEYCHAIN_SERVICE = "homelab-homeassistant-api-token"


class Direction(enum.Enum):
    """Which way a file travels between the clone and lab."""

    # Round-trip: GUI edits are pulled back, clone edits are pushed.
    BOTH = "both"
    # Repo-owned artifacts the HA UI can't edit, so a pull must not clobber the
    # clone copy with the host's stub.
    PUSH = "push"
    # HA-owned `.storage` captured for history only. Never written back: HA holds
    # these stores in memory and rewrites them on shutdown, so an upload to a
    # running instance would be silently undone.
    PULL = "pull"


@dataclass(frozen=True)
class SyncFile:
    rel: str
    reload_service: str | None
    direction: Direction

    @property
    def pull(self) -> bool:
        return self.direction is not Direction.PUSH

    @property
    def push(self) -> bool:
        return self.direction is not Direction.PULL


# Spec tuple: (glob, reload_service, direction).
# An explicit homeassistant.restart covers every changed file; otherwise the
# touched domains reload individually. None means no service call: YAML-mode
# Lovelace dashboards are re-read on the next dashboard load.
SYNC_SPEC = [
    ("automations.yaml", "automation.reload", Direction.BOTH),
    ("scripts.yaml", "script.reload", Direction.BOTH),
    ("scenes.yaml", "scene.reload", Direction.BOTH),
    ("templates.yaml", "template.reload", Direction.BOTH),
    ("input_numbers.yaml", "input_number.reload", Direction.BOTH),
    ("input_selects.yaml", "input_select.reload", Direction.BOTH),
    ("timers.yaml", "timer.reload", Direction.BOTH),
    # The `counter` integration registers no reload service (only increment/
    # decrement/reset/set_value), so a new or changed counter only loads on
    # restart — homeassistant.restart covers the whole file.
    ("counters.yaml", "homeassistant.restart", Direction.BOTH),
    # `statistics` sensors have no hot reload, so restart for the whole file.
    ("sensors.yaml", "homeassistant.restart", Direction.BOTH),
    # The legacy `plant:` integration has no hot reload service.
    ("plants.yaml", "homeassistant.restart", Direction.BOTH),
    # climate_template is a legacy `climate:` platform — no hot reload, restart.
    ("climate.yaml", "homeassistant.restart", Direction.BOTH),
    # HA returns a clear API warning if this reload service is unavailable.
    ("custom_templates/*", "homeassistant.reload_custom_templates", Direction.BOTH),
    # Reload automations so blueprint consumers re-read changed sources.
    ("blueprints/automation/*", "automation.reload", Direction.BOTH),
    # YAML-mode Lovelace dashboards: repo-owned (read-only in the HA UI), so
    # push-only; HA re-reads them on the next load, so no reload service.
    ("dashboards/*", None, Direction.PUSH),
    # Bubble Card modules: fetched by the browser from /local on every
    # dashboard load, so no reload service.
    ("www/bubble/*", None, Direction.PUSH),
    # The household dashboard stays UI-edited; capture it (and the frontend
    # resources its custom cards need) so edits have a history.
    (".storage/lovelace.dashboard_test", None, Direction.PULL),
    (".storage/lovelace_resources", None, Direction.PULL),
]


def enumerate_files(for_pull: bool = False) -> list[SyncFile]:
    """Clone files taking part in a pull (for_pull) or a push.

    A literal pull path is listed even before its first capture creates it in
    the clone; globs only match what the clone already holds.
    """
    files: list[SyncFile] = []
    for pattern, reload_service, direction in SYNC_SPEC:
        spec = SyncFile(pattern, reload_service, direction)
        if not (spec.pull if for_pull else spec.push):
            continue
        if for_pull and not glob.has_magic(pattern):
            files.append(spec)
            continue
        files += [
            SyncFile(path.relative_to(CLONE).as_posix(), reload_service, direction)
            for path in sorted(CLONE.glob(pattern))
            if path.is_file()
        ]
    return files


def host_file(rel: str) -> bytes | None:
    r = subprocess.run(
        ["ssh", HOST, f"sudo cat {HOST_DIR}/{rel} 2>/dev/null"],
        capture_output=True,
        check=False,
    )
    return r.stdout if r.returncode == 0 else None


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


def fail(message: str) -> NoReturn:
    sys.exit(f"ha:sync: {message}")


def indent_block(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


def describe_process_error(error: subprocess.CalledProcessError) -> str:
    """Render a failed subprocess as an operator-readable message.

    sh() captures output, so an uncaught CalledProcessError prints a traceback
    with the command's own diagnostics swallowed -- the one thing needed to
    understand the failure. Surface the command and its stderr instead.
    """
    command = " ".join(str(part) for part in error.cmd)
    detail = (error.stderr or error.stdout or "").strip()
    rendered = f"command failed (exit {error.returncode}): {command}"
    return f"{rendered}\n{indent_block(detail)}" if detail else rendered


def sync_clone_with_origin(rebase: bool = False) -> None:
    """Bring the clone up to date with origin before comparing it to the host.

    Fast-forward only by default: a merge commit here would make
    last_synced_to_host ambiguous about which tree is actually on lab. A clone
    holding both local commits and new origin commits cannot fast-forward, and
    that is the case --rebase exists for -- it replays the local commits on top.
    """
    strategy = "--rebase" if rebase else "--ff-only"
    result = sh(["git", "pull", strategy, "--quiet"], cwd=CLONE, check=False)
    if result.returncode == 0:
        return
    detail = indent_block((result.stderr or result.stdout or "").strip())
    if rebase:
        # Leave no half-applied rebase for the next invocation to trip over.
        sh(["git", "rebase", "--abort"], cwd=CLONE, check=False)
        fail(
            f"rebasing the clone onto origin failed; it has been left unchanged.\n{detail}\nresolve {CLONE} by hand, then re-run."
        )
    fail(
        f"the ha_gui_config clone has diverged from origin and cannot fast-forward.\n{detail}\n"
        f"re-run with --rebase to replay the clone's local commits onto origin, "
        f"or reconcile {CLONE} by hand."
    )


def blob_at(ref: str, filename: str) -> bytes | None:
    """File content at a Git ref, or None if the file does not exist there."""
    r = subprocess.run(
        ["git", "show", f"{ref}:{filename}"],
        cwd=CLONE,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def worktree_file(filename: str) -> bytes | None:
    """File content from the clone working tree, or None if absent.

    Used by `push --dry-run`, which doesn't auto-commit, so the comparison point
    is the working tree (uncommitted edits included) rather than the HEAD blob.
    """
    try:
        return (CLONE / filename).read_bytes()
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
    result = sh(["git", "push", "origin", "main"], cwd=CLONE, check=False)
    if result.returncode != 0:
        detail = indent_block((result.stderr or result.stdout or "").strip())
        fail(
            f"the clone commit succeeded but `git push origin main` was rejected.\n{detail}\n"
            f"the commit is safe locally; re-run with --rebase once origin is reconciled."
        )
    return True


def resolve_ref(ref: str) -> str | None:
    r = sh(["git", "rev-parse", "--verify", "--quiet", ref], cwd=CLONE, check=False)
    return r.stdout.strip() or None


def advance_synced_tag(commit: str = "HEAD") -> None:
    """Publish the applied commit before moving the local marker."""
    sh(["git", "push", "origin", "--force", f"{commit}:refs/tags/{SYNCED_TAG}"], cwd=CLONE)
    sh(["git", "tag", "-f", SYNCED_TAG, commit], cwd=CLONE)


def host_snapshot_tree(base: str) -> str:
    """Tree of `base` with every pulled file replaced by its copy on lab.

    Built in a throwaway index so neither the working tree nor local commits
    are touched; a pulled file missing on lab aborts the pull.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": f"{tmp}/index"}

        def git(*args: str, content: bytes | None = None) -> str:
            result = subprocess.run(["git", *args], cwd=CLONE, env=env, input=content, capture_output=True, check=True)
            return result.stdout.decode().strip()

        git("read-tree", base)
        for file in enumerate_files(for_pull=True):
            content = host_file(file.rel)
            if content is None:
                fail(f"cannot read {HOST_DIR}/{file.rel} on {HOST}")
            blob = git("hash-object", "-w", "--stdin", content=content)
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{file.rel}")
        return git("write-tree")


def upload_to_host(files: list[SyncFile]) -> None:
    """Upload via /tmp, then sudo install with owner, mode, and backup."""
    # Match the role's 0644 stubs so sync and converge do not fight over modes.
    # Synced GUI YAML contains no secrets; those stay in secrets.yaml.
    pid = os.getpid()
    for file in files:
        safe = file.rel.replace("/", "_")
        tmp_remote = f"/tmp/.ha_sync_{pid}_{safe}"
        sh(["scp", "-q", str(CLONE / file.rel), f"{HOST}:{tmp_remote}"])
        # The role owns HOST_DIR at 0750; only create nested directories here.
        parent = file.rel.rpartition("/")[0]
        ensure_parent = (
            f"sudo install -d -o homeassistant -g homeassistant -m 0755 {HOST_DIR}/{parent} && " if parent else ""
        )
        sh(
            [
                "ssh",
                HOST,
                f"{ensure_parent}sudo install -o homeassistant -g homeassistant -m 0644 -b {tmp_remote} {HOST_DIR}/{file.rel} && sudo rm -f {tmp_remote}",
            ]
        )


def ha_api_token() -> str:
    """Read the HA bearer from the environment or the macOS login Keychain."""
    token = os.environ.get("HA_API_TOKEN", "").strip()
    if token and not token.startswith("op://"):
        return token
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(), "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    sys.exit(
        "refusing: no Home Assistant API token available; nothing uploaded. "
        f"Add it to Keychain with `security add-generic-password -a {getpass.getuser()} -s {KEYCHAIN_SERVICE} -w` "
        "or set HA_API_TOKEN to a literal token."
    )


def _ha_post(service: str, token: str) -> None:
    """POST /api/services/<domain>/<action> with the bearer."""
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
    except urllib.error.URLError as e:
        raise RuntimeError(f"{service} failed; retry `mise run ha:sync push` after fixing HA: {e}") from e


def _print_diff_header(label: str) -> None:
    print(f"\n\033[1;34m=== {label} ===\033[0m", flush=True)


def show_push_diff(changed: dict[SyncFile, bytes | None]) -> None:
    """For each file about to be pushed, print a colored diff host->HEAD."""
    for file, host_bytes in changed.items():
        with tempfile.NamedTemporaryFile(suffix=f"_{Path(file.rel).name}") as host_tmp:
            host_tmp.write(host_bytes or b"")
            host_tmp.flush()
            status = "new on host" if host_bytes is None else "modified on host"
            _print_diff_header(f"push: {file.rel} ({status})")
            # --no-index exits 1 when files differ; ignore.
            subprocess.run(
                ["git", "diff", "--color=always", "--no-index", "--", host_tmp.name, str(CLONE / file.rel)],
                check=False,
            )


def do_pull(rebase: bool = False) -> None:
    """Capture lab's edits on top of origin, then rebase local commits onto them.

    The capture commit sits directly on origin/main, so last_synced_to_host can
    name a commit whose synced files match lab exactly even while unpushed local
    commits exist. Those are replayed on top and stay local until the next push.
    """
    assert_clone_present()
    assert_clean_working_tree()
    sh(["git", "fetch", "--tags", "--force"], cwd=CLONE)
    sync_clone_with_origin(rebase)
    upstream = sh(["git", "rev-parse", "origin/main"], cwd=CLONE).stdout.strip()
    if resolve_ref(f"refs/tags/{SYNCED_TAG}") is not None:
        pending = [
            file.rel for file in enumerate_files() if blob_at(SYNCED_TAG, file.rel) != blob_at(upstream, file.rel)
        ]
        if pending:
            sys.exit(
                f"refusing: origin changes await push or reload ({', '.join(pending)}); "
                "run `mise run ha:sync push` before pulling."
            )
    tree = host_snapshot_tree(upstream)
    capture = upstream
    if tree != sh(["git", "rev-parse", f"{upstream}^{{tree}}"], cwd=CLONE).stdout.strip():
        _print_diff_header("pull: host-side changes about to be committed")
        subprocess.run(["git", "diff", "--color=always", upstream, tree], cwd=CLONE, check=False)
        # Unlike commit and rebase, commit-tree ignores commit.gpgsign.
        signing = sh(["git", "config", "--type=bool", "--default=false", "commit.gpgsign"], cwd=CLONE).stdout.strip()
        sign = ["-S"] if signing == "true" else []
        capture = sh(
            ["git", "commit-tree", *sign, tree, "-p", upstream, "-m", "pull: capture GUI edits from lab"], cwd=CLONE
        ).stdout.strip()
        sh(["git", "push", "origin", f"{capture}:refs/heads/main"], cwd=CLONE)
        print("pull: committed GUI edits + pushed")
    if resolve_ref(f"refs/tags/{SYNCED_TAG}") != capture:
        advance_synced_tag(capture)
        print(f"pull: advanced {SYNCED_TAG}")
    result = sh(["git", "rebase", "--quiet", capture], cwd=CLONE, check=False)
    if result.returncode != 0:
        # Leave no half-applied rebase for the next invocation to trip over.
        sh(["git", "rebase", "--abort"], cwd=CLONE, check=False)
        fail(
            f"lab's edits are captured on origin, but replaying local commits onto them failed; "
            f"the local branch is unchanged.\n{indent_block((result.stderr or result.stdout).strip())}\n"
            f"run `git rebase origin/main` in {CLONE}, resolve, then `mise run ha:sync push`."
        )


def do_push(dry_run: bool = False, rebase: bool = False) -> None:
    assert_clone_present()
    if not dry_run:
        sh(["git", "fetch", "--tags", "--force"], cwd=CLONE)
        sync_clone_with_origin(rebase)
        # Auto-commit any working-tree edits so `ha:sync push` works straight
        # from a direct file edit in the clone without a manual git commit.
        if commit_and_push("push: local edits"):
            print("push: committed local edits")
    if resolve_ref(f"refs/tags/{SYNCED_TAG}") is None:
        sys.exit(f"refusing: no {SYNCED_TAG} tag. Run `mise run ha:pull` once to establish the baseline.")
    # Per-file state model:
    #   tag_bytes  -- content at last_synced_to_host, or None if new to sync
    #   head_bytes -- content at clone HEAD (always exists by definition)
    #   host_bytes -- content on lab, or None if not deployed yet
    # diverged   = host content differs from both tag and HEAD
    # changed    = host content differs from HEAD (missing-on-host counts)
    # pending    = clone content differs from the last successfully reloaded tag
    diverged: list[str] = []
    changed: dict[SyncFile, bytes | None] = {}
    pending: list[SyncFile] = []
    for file in enumerate_files():
        # dry-run compares the working tree (uncommitted edits included); a real
        # push has already folded those into HEAD via commit_and_push.
        head_bytes = worktree_file(file.rel) if dry_run else blob_at("HEAD", file.rel)
        tag_bytes = blob_at(SYNCED_TAG, file.rel)
        host_bytes = host_file(file.rel)
        # Push-only files (pull=False) are repo-authoritative: the host copy is
        # never captured back (do_pull skips them), so a host/tag mismatch must
        # not block the push -- a pull can't resolve it (it would deadlock: the
        # role seeds a dashboards/ placeholder that differs from the repo). Only
        # guard divergence for round-tripped files, where a stale host/GUI edit
        # would otherwise be clobbered.
        if file.pull and tag_bytes is not None and host_bytes is not None and host_bytes not in (tag_bytes, head_bytes):
            diverged.append(file.rel)
            continue
        if host_bytes != head_bytes:
            changed[file] = host_bytes
        if tag_bytes != head_bytes:
            pending.append(file)
    if diverged:
        msg = [f"refusing: host diverged from {SYNCED_TAG} (GUI/host edited since last sync):"]
        msg += [f"  {f}" for f in diverged]
        msg.append("\nrun `mise run ha:pull` to capture host-side edits, then retry push.")
        sys.exit("\n".join(msg))
    if not changed and not pending:
        if not dry_run and resolve_ref(f"refs/tags/{SYNCED_TAG}") != resolve_ref("HEAD"):
            advance_synced_tag()
        print("push: HEAD matches host, nothing to do")
        return
    files = list(changed)
    reloads = {file.reload_service for file in [*files, *pending]}
    services = (
        ["homeassistant.restart"]
        if "homeassistant.restart" in reloads
        else sorted(reload for reload in reloads if reload)
    )
    relpaths = [file.rel for file in files]
    show_push_diff(changed)
    validate_syntax([file.rel for file in dict.fromkeys([*files, *pending])])
    if dry_run:
        reload_desc = ", ".join(services) or "none"
        print(f"\n\033[1;33mdry-run\033[0m: would upload {relpaths}")
        print(f"\033[1;33mdry-run\033[0m: would advance {SYNCED_TAG} and trigger: {reload_desc}")
        print("\033[1;33mdry-run\033[0m: nothing written to host, no git refs moved, HA not reloaded.")
        return
    token = ha_api_token() if services else None
    if files:
        upload_to_host(files)
        print(f"push: uploaded {relpaths}")
    if services == ["homeassistant.restart"]:
        print("at least one changed file requires a restart -- restarting homeassistant")
    for service in services:
        assert token is not None
        _ha_post(service, token)
    advance_synced_tag()
    # YAML-mode dashboards need no service call; nudge the operator that the
    # change is live but only visible after a browser refresh.
    if any(file.reload_service is None for file in changed):
        print("note: dashboard change is live -- refresh the browser to see it")


def main() -> None:
    raw = sys.argv[1:]
    dry_run = "--dry-run" in raw
    rebase = "--rebase" in raw
    positional = [a for a in raw if a not in ("--dry-run", "--rebase")]
    mode = (positional[0] if positional else "sync").strip()
    if dry_run and mode not in ("push", "sync"):
        sys.exit("--dry-run only applies to push")
    if dry_run and rebase:
        sys.exit("--rebase has no effect with --dry-run, which never updates the clone")
    if mode not in ("pull", "push", "sync"):
        sys.exit(f"unknown mode {mode!r}; use pull | push | sync")
    try:
        if dry_run:
            # Pull mutates by committing host edits, so dry-run previews push only.
            if mode == "sync":
                print("dry-run: skipping pull; previewing push only")
            do_push(dry_run=True)
            return
        if mode in ("pull", "sync"):
            do_pull(rebase)
        if mode in ("push", "sync"):
            do_push(rebase=rebase)
    except subprocess.CalledProcessError as error:
        fail(describe_process_error(error))


if __name__ == "__main__":
    main()
