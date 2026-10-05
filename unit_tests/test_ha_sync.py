"""Check GUI YAML sync deployment and credential handling."""

import shutil
import subprocess

import pytest
from conftest import load_repo_module


def test_plants_require_home_assistant_restart() -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_plants_test")

    assert ("plants.yaml", "homeassistant.restart", sync.Direction.BOTH) in sync.SYNC_SPEC


def test_pull_only_files_are_captured_but_never_pushed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_pull_only_test")
    monkeypatch.setattr(sync, "CLONE", tmp_path)
    monkeypatch.setattr(
        sync,
        "SYNC_SPEC",
        [
            ("automations.yaml", "automation.reload", sync.Direction.BOTH),
            ("www/bubble/*", None, sync.Direction.PUSH),
            (".storage/lovelace.dashboard_test", None, sync.Direction.PULL),
        ],
    )
    (tmp_path / "automations.yaml").write_text("[]\n")
    (tmp_path / "www/bubble").mkdir(parents=True)
    (tmp_path / "www/bubble/bubble-modules.yaml").write_text("{}\n")

    # Not yet in the clone: a literal pull path is still listed so the first
    # pull can capture it.
    pulled = [file.rel for file in sync.enumerate_files(for_pull=True)]
    assert pulled == ["automations.yaml", ".storage/lovelace.dashboard_test"]

    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage/lovelace.dashboard_test").write_text("{}\n")
    pushed = [file.rel for file in sync.enumerate_files()]
    assert pushed == ["automations.yaml", "www/bubble/bubble-modules.yaml"]


def test_upload_creates_only_nested_parent_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_API_TOKEN", raising=False)
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_upload_test")
    commands: list[list[str]] = []
    monkeypatch.setattr(sync, "sh", commands.append)

    sync.upload_to_host(
        [
            sync.SyncFile("automations.yaml", "automation.reload", sync.Direction.BOTH),
            sync.SyncFile("dashboards/climate.yaml", None, sync.Direction.PUSH),
        ]
    )

    assert len(commands) == 4
    top_level = commands[1][2]
    nested = commands[3][2]
    assert "install -d" not in top_level
    assert "-m 0644 -b" in top_level
    assert nested.startswith(
        "sudo install -d -o homeassistant -g homeassistant -m 0755 /mnt/services/homeassistant/dashboards && "
    )
    assert "-m 0644 -b" in nested


def test_token_reads_keychain_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_keychain_test")
    monkeypatch.delenv("HA_API_TOKEN", raising=False)
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    commands: list[list[str]] = []

    def keychain(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "example-token\n", "")

    monkeypatch.setattr(sync.subprocess, "run", keychain)
    assert sync.ha_api_token() == "example-token"
    assert commands == [
        ["/usr/bin/security", "find-generic-password", "-a", sync.getpass.getuser(), "-s", sync.KEYCHAIN_SERVICE, "-w"]
    ]


def test_unresolved_op_reference_falls_back_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_op_ref_test")
    monkeypatch.setenv("HA_API_TOKEN", "op://example")
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "keychain-token\n", ""),
    )
    assert sync.ha_api_token() == "keychain-token"


def test_push_retries_reload_after_upload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_retry_test")
    file = sync.SyncFile("scripts.yaml", "script.reload", sync.Direction.BOTH)
    host_bytes = [b"old", b"new"]
    uploads: list[list[object]] = []
    reloads: list[tuple[str, str]] = []
    tags: list[str] = []
    monkeypatch.setattr(sync, "assert_clone_present", lambda: None)
    monkeypatch.setattr(sync, "sh", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "sync_clone_with_origin", lambda rebase=False: None)
    monkeypatch.setattr(sync, "commit_and_push", lambda message: False)
    monkeypatch.setattr(sync, "resolve_ref", lambda ref: "old" if ref != "HEAD" else "new")
    monkeypatch.setattr(sync, "enumerate_files", lambda: [file])
    monkeypatch.setattr(sync, "blob_at", lambda ref, rel: b"old" if ref == sync.SYNCED_TAG else b"new")
    monkeypatch.setattr(sync, "host_file", lambda rel: host_bytes[0])
    monkeypatch.setattr(sync, "show_push_diff", lambda changed: None)
    monkeypatch.setattr(sync, "validate_syntax", lambda paths: None)
    monkeypatch.setattr(sync, "ha_api_token", lambda: "example-token")
    monkeypatch.setattr(sync, "upload_to_host", lambda files: uploads.append(files))
    monkeypatch.setattr(sync, "advance_synced_tag", lambda: tags.append("advanced"))

    def reload(service: str, token: str) -> None:
        reloads.append((service, token))
        if len(reloads) == 1:
            raise RuntimeError("reload failed")

    monkeypatch.setattr(sync, "_ha_post", reload)
    with pytest.raises(RuntimeError, match="reload failed"):
        sync.do_push()
    assert uploads == [[file]]
    assert tags == []

    host_bytes[0] = b"new"
    sync.do_push()
    assert uploads == [[file]]
    assert reloads == [("script.reload", "example-token"), ("script.reload", "example-token")]
    assert tags == ["advanced"]


def test_push_without_token_never_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_missing_token_test")
    file = sync.SyncFile("scripts.yaml", "script.reload", sync.Direction.BOTH)
    monkeypatch.setattr(sync, "assert_clone_present", lambda: None)
    monkeypatch.setattr(sync, "sh", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, "sync_clone_with_origin", lambda rebase=False: None)
    monkeypatch.setattr(sync, "commit_and_push", lambda message: False)
    monkeypatch.setattr(sync, "resolve_ref", lambda ref: "old")
    monkeypatch.setattr(sync, "enumerate_files", lambda: [file])
    monkeypatch.setattr(sync, "blob_at", lambda ref, rel: b"old" if ref == sync.SYNCED_TAG else b"new")
    monkeypatch.setattr(sync, "host_file", lambda rel: b"old")
    monkeypatch.setattr(sync, "show_push_diff", lambda changed: None)
    monkeypatch.setattr(sync, "validate_syntax", lambda paths: None)
    monkeypatch.setattr(sync, "ha_api_token", lambda: (_ for _ in ()).throw(SystemExit("no token")))
    monkeypatch.setattr(sync, "upload_to_host", lambda files: pytest.fail("uploaded without token"))
    with pytest.raises(SystemExit, match="no token"):
        sync.do_push()


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def synced_clone(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A clone whose origin/main and last_synced_to_host match a fake lab host."""
    sync = load_repo_module("mise-tasks/ha/sync.py", name=f"ha_sync_pull_{tmp_path.name}")
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        _git(clone, "config", key, value)
    host = {"automations.yaml": b"- id: a\n", "scripts.yaml": b"{}\n"}
    for rel, content in host.items():
        (clone / rel).write_bytes(content)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "baseline")
    _git(clone, "push", "--quiet", "origin", "main")
    _git(clone, "tag", sync.SYNCED_TAG)
    _git(clone, "push", "--quiet", "origin", sync.SYNCED_TAG)
    monkeypatch.setattr(sync, "CLONE", clone)
    monkeypatch.setattr(
        sync,
        "SYNC_SPEC",
        [
            ("automations.yaml", "automation.reload", sync.Direction.BOTH),
            ("scripts.yaml", "script.reload", sync.Direction.BOTH),
        ],
    )
    monkeypatch.setattr(sync, "host_file", host.get)
    return sync, clone, host


def test_pull_rebases_local_commits_onto_captured_host_edits(synced_clone) -> None:
    sync, clone, host = synced_clone
    (clone / "automations.yaml").write_text("- id: local\n")
    _git(clone, "commit", "--quiet", "-am", "local edit")
    host["scripts.yaml"] = b"gui: edit\n"

    sync.do_pull()

    capture = _git(clone, "rev-parse", "origin/main")
    assert _git(clone, "rev-parse", sync.SYNCED_TAG) == capture
    assert _git(clone, "ls-remote", "origin", f"refs/tags/{sync.SYNCED_TAG}").split()[0] == capture
    assert _git(clone, "show", f"{capture}:scripts.yaml") == "gui: edit"
    assert _git(clone, "show", f"{capture}:automations.yaml") == "- id: a"
    assert _git(clone, "rev-parse", "HEAD~1") == capture
    assert _git(clone, "log", "-1", "--format=%s") == "local edit"
    assert (clone / "scripts.yaml").read_text() == "gui: edit\n"
    assert (clone / "automations.yaml").read_text() == "- id: local\n"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen unavailable")
def test_pull_signs_the_capture_when_commits_are_signed(synced_clone, tmp_path) -> None:
    sync, clone, host = synced_clone
    key = tmp_path / "signing_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    for config_key, value in (("gpg.format", "ssh"), ("user.signingkey", str(key)), ("commit.gpgsign", "true")):
        _git(clone, "config", config_key, value)
    host["scripts.yaml"] = b"gui: edit\n"

    sync.do_pull()

    assert "gpgsig" in _git(clone, "cat-file", "commit", "origin/main")


def test_pull_conflict_keeps_capture_and_local_branch(synced_clone) -> None:
    sync, clone, host = synced_clone
    (clone / "automations.yaml").write_text("- id: local\n")
    _git(clone, "commit", "--quiet", "-am", "local edit")
    local = _git(clone, "rev-parse", "HEAD")
    host["automations.yaml"] = b"- id: gui\n"

    with pytest.raises(SystemExit, match="git rebase origin/main"):
        sync.do_pull()

    assert _git(clone, "rev-parse", "HEAD") == local
    assert _git(clone, "status", "--porcelain") == ""
    assert _git(clone, "show", "origin/main:automations.yaml") == "- id: gui"
    assert _git(clone, "rev-parse", sync.SYNCED_TAG) == _git(clone, "rev-parse", "origin/main")


def test_pull_refuses_while_origin_awaits_push(synced_clone) -> None:
    sync, clone, host = synced_clone
    (clone / "scripts.yaml").write_text("pushed: but not deployed\n")
    _git(clone, "commit", "--quiet", "-am", "undeployed")
    _git(clone, "push", "--quiet", "origin", "main")
    host["scripts.yaml"] = b"gui: edit\n"

    with pytest.raises(SystemExit, match="origin changes await push or reload"):
        sync.do_pull()


def test_diverged_clone_names_the_rebase_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_diverged_test")
    monkeypatch.setattr(
        sync,
        "sh",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="fatal: Not possible to fast-forward, aborting."
        ),
    )
    with pytest.raises(SystemExit) as caught:
        sync.sync_clone_with_origin()
    message = str(caught.value)
    assert "diverged from origin" in message
    assert "--rebase" in message
    assert "Not possible to fast-forward" in message


def test_rebase_flag_switches_the_pull_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_rebase_strategy_test")
    seen: list[list[str]] = []

    def run(cmd, cwd=None, check=True):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sync, "sh", run)
    sync.sync_clone_with_origin(rebase=True)
    assert seen == [["git", "pull", "--rebase", "--quiet"]]
    seen.clear()
    sync.sync_clone_with_origin()
    assert seen == [["git", "pull", "--ff-only", "--quiet"]]


def test_failed_rebase_aborts_and_leaves_the_clone_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_rebase_abort_test")
    seen: list[list[str]] = []

    def run(cmd, cwd=None, check=True):
        seen.append(cmd)
        code = 1 if cmd[:2] == ["git", "pull"] else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="CONFLICT in automations.yaml")

    monkeypatch.setattr(sync, "sh", run)
    with pytest.raises(SystemExit) as caught:
        sync.sync_clone_with_origin(rebase=True)
    assert ["git", "rebase", "--abort"] in seen
    assert "CONFLICT in automations.yaml" in str(caught.value)


def test_subprocess_failures_report_the_command_not_a_traceback() -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_error_render_test")
    error = subprocess.CalledProcessError(
        128, ["git", "push", "origin", "main"], output="", stderr="fatal: remote rejected"
    )
    rendered = sync.describe_process_error(error)
    assert "git push origin main" in rendered
    assert "exit 128" in rendered
    assert "fatal: remote rejected" in rendered


def test_rebase_is_rejected_with_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_flag_combo_test")
    monkeypatch.setattr(sync.sys, "argv", ["sync.py", "push", "--dry-run", "--rebase"])
    with pytest.raises(SystemExit, match="no effect with --dry-run"):
        sync.main()
