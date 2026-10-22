"""Check GUI YAML sync deployment and credential handling."""

import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
    assert f"-m 0644 -b {sync.BACKUP_SUFFIX} " in top_level
    assert nested.startswith(
        "sudo install -d -o homeassistant -g homeassistant -m 0755 /mnt/services/homeassistant/dashboards && "
    )
    assert "-m 0644 -b" in nested


def test_restore_reinstalls_previous_content_and_removes_added_files(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_restore_test")
    commands: list[list[str]] = []
    staged: list[bytes] = []

    def record(cmd: list[str]) -> None:
        commands.append(cmd)
        if cmd[0] == "scp":
            staged.append(Path(cmd[2]).read_bytes())

    monkeypatch.setattr(sync, "sh", record)

    sync.restore_host({"automations.yaml": b"- id: a\n", "blueprints/automation/new.yaml": None})

    assert staged == [b"- id: a\n"]
    assert commands[1][2].endswith("/mnt/services/homeassistant/automations.yaml && sudo rm -f " + commands[0][3][4:])
    assert commands[2] == ["ssh", "lab", "sudo rm -f /mnt/services/homeassistant/blueprints/automation/new.yaml"]


def test_upload_backups_match_the_ansible_backup_pruner() -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_backup_suffix_test")
    pruner = (sync.REPO_ROOT / "roles/cleanup/files/prune_ansible_backups").read_text()
    regex = re.search(r"-regex '([^']+)'", pruner)
    assert regex is not None
    expanded = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; printf %s {sync.BACKUP_SUFFIX.removeprefix('-S ')}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert re.fullmatch(regex.group(1), f"/mnt/services/homeassistant/automations.yaml{expanded}"), expanded


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


def test_host_read_tells_a_missing_file_from_a_failed_read(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sync = load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_host_file_test")
    monkeypatch.setattr(sync, "HOST_DIR", str(tmp_path))
    run = subprocess.run

    def local_ssh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # Run the remote command here, without ssh and sudo.
        assert cmd[:2] == ["ssh", sync.HOST]
        return run(["sh", "-c", cmd[2].removeprefix("sudo ")], **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", local_ssh)
    (tmp_path / "scripts.yaml").write_bytes(b"{}\n")
    (tmp_path / "unreadable.yaml").mkdir()

    assert sync.host_file("scripts.yaml") == b"{}\n"
    assert sync.host_file("automations.yaml") is None
    with pytest.raises(SystemExit, match="Is a directory"):
        sync.host_file("unreadable.yaml")


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

    def upload(files) -> None:
        for file in files:
            host[file.rel] = (clone / file.rel).read_bytes()

    monkeypatch.setattr(sync, "upload_to_host", upload)

    def restore(previous: dict[str, bytes | None]) -> None:
        for rel, content in previous.items():
            if content is None:
                host.pop(rel, None)
            else:
                host[rel] = content

    monkeypatch.setattr(sync, "restore_host", restore)
    return sync, clone, host


def _stub_ha(monkeypatch: pytest.MonkeyPatch, sync) -> list[str]:
    """Accept every configuration check and record the services called."""
    reloads: list[str] = []
    monkeypatch.setattr(sync, "ha_api_token", lambda: "example-token")
    monkeypatch.setattr(sync, "check_host_config", lambda token: None)
    monkeypatch.setattr(sync, "_ha_post", lambda service, token: reloads.append(service))
    return reloads


def _record_uploads(monkeypatch: pytest.MonkeyPatch, sync) -> list[list[str]]:
    uploads: list[list[str]] = []
    upload = sync.upload_to_host

    def record(files) -> None:
        uploads.append([file.rel for file in files])
        upload(files)

    monkeypatch.setattr(sync, "upload_to_host", record)
    return uploads


def _synced_commit(clone, sync) -> tuple[str, str]:
    """The synced tag locally and on origin."""
    return (
        _git(clone, "rev-parse", sync.SYNCED_TAG),
        _git(clone, "ls-remote", "origin", f"refs/tags/{sync.SYNCED_TAG}").split()[0],
    )


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


def test_push_publishes_hand_made_commits_before_tagging(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, _host = synced_clone
    (clone / "scripts.yaml").write_text("local: edit\n")
    _git(clone, "commit", "--quiet", "-am", "hand-made commit")
    _stub_ha(monkeypatch, sync)

    sync.do_push()

    head = _git(clone, "rev-parse", "HEAD")
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _git(clone, "ls-remote", "origin", f"refs/tags/{sync.SYNCED_TAG}").split()[0] == head


def test_push_retries_reload_after_reload_failure(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, host = synced_clone
    baseline = _git(clone, "rev-parse", "HEAD")
    _stub_ha(monkeypatch, sync)
    uploads = _record_uploads(monkeypatch, sync)
    reloads: list[str] = []

    def reload(service: str, token: str) -> None:
        reloads.append(service)
        if len(reloads) == 1:
            raise RuntimeError("reload failed")

    monkeypatch.setattr(sync, "_ha_post", reload)
    (clone / "scripts.yaml").write_text("new: script\n")

    with pytest.raises(RuntimeError, match="reload failed"):
        sync.do_push()
    assert host["scripts.yaml"] == b"new: script\n"
    assert _synced_commit(clone, sync) == (baseline, baseline)

    sync.do_push()

    head = _git(clone, "rev-parse", "HEAD")
    assert uploads == [["scripts.yaml"]]
    assert reloads == ["script.reload", "script.reload"]
    assert _synced_commit(clone, sync) == (head, head)


def test_push_reloads_custom_templates_before_their_consumers(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, _host = synced_clone
    monkeypatch.setattr(
        sync,
        "SYNC_SPEC",
        [
            *sync.SYNC_SPEC,
            ("scenes.yaml", "scene.reload", sync.Direction.BOTH),
            ("custom_templates/*", "homeassistant.reload_custom_templates", sync.Direction.BOTH),
        ],
    )
    reloads = _stub_ha(monkeypatch, sync)
    (clone / "automations.yaml").write_text("- id: b\n")
    (clone / "scenes.yaml").write_text("[]\n")
    (clone / "custom_templates").mkdir()
    (clone / "custom_templates/macros.jinja").write_text("{% macro greet() %}hi{% endmacro %}\n")

    sync.do_push()

    assert reloads == ["homeassistant.reload_custom_templates", "scene.reload", "automation.reload"]


def test_push_skips_reload_when_ha_rejects_the_config(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, host = synced_clone
    baseline = _git(clone, "rev-parse", "HEAD")
    check_host_config = sync.check_host_config
    reloads = _stub_ha(monkeypatch, sync)
    monkeypatch.setattr(sync, "check_host_config", check_host_config)
    verdict = {"result": "invalid", "errors": "Invalid config for 'automation'"}
    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(json.dumps(verdict).encode()))
    (clone / "automations.yaml").write_text("- id: b\n")

    with pytest.raises(SystemExit, match="Invalid config for 'automation'"):
        sync.do_push()
    # The next HA restart must not load what HA just rejected.
    assert host["automations.yaml"] == b"- id: a\n"
    assert sync.resolve_ref(sync.UPLOAD_REF) is None
    assert reloads == []
    assert _synced_commit(clone, sync) == (baseline, baseline)

    # HA itself was at fault; once it accepts the same upload, the retry reloads.
    verdict = {"result": "valid", "errors": None}
    sync.do_push()

    head = _git(clone, "rev-parse", "HEAD")
    assert reloads == ["automation.reload"]
    assert _synced_commit(clone, sync) == (head, head)


def test_push_restores_lab_after_a_failed_upload(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, host = synced_clone
    reloads = _stub_ha(monkeypatch, sync)
    upload = sync.upload_to_host

    def fail_midway(files) -> None:
        upload(files[:1])
        raise subprocess.CalledProcessError(1, ["scp"])

    monkeypatch.setattr(sync, "upload_to_host", fail_midway)
    (clone / "automations.yaml").write_text("- id: b\n")
    (clone / "scripts.yaml").write_text("new: script\n")

    with pytest.raises(subprocess.CalledProcessError):
        sync.do_push()

    assert host == {"automations.yaml": b"- id: a\n", "scripts.yaml": b"{}\n"}
    assert sync.resolve_ref(sync.UPLOAD_REF) is None
    assert reloads == []


def test_push_refuses_a_diverged_host_before_publishing_local_edits(synced_clone) -> None:
    sync, clone, host = synced_clone
    head = _git(clone, "rev-parse", "HEAD")
    (clone / "scripts.yaml").write_text("local: edit\n")
    host["automations.yaml"] = b"- id: gui\n"

    with pytest.raises(SystemExit, match="host diverged") as refusal:
        sync.do_push()

    assert _git(clone, "rev-parse", "HEAD") == head
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _git(clone, "status", "--porcelain") == "M scripts.yaml"
    assert "commit the local edits" in str(refusal.value)

    # The advertised recovery: commit locally, then pull.
    _git(clone, "commit", "--quiet", "-am", "local edit")
    sync.do_pull()
    capture = _git(clone, "rev-parse", "origin/main")
    assert _git(clone, "show", f"{capture}:automations.yaml") == "- id: gui"
    assert _git(clone, "rev-parse", "HEAD~1") == capture


@pytest.mark.parametrize(
    ("edit", "refusal"),
    [
        ("broken: [yaml\n", "syntax errors"),
        ("local: edit\n", "no Home Assistant API token"),
    ],
    ids=["syntax_error", "missing_token"],
)
def test_push_refuses_local_input_before_publishing(
    synced_clone, monkeypatch: pytest.MonkeyPatch, edit: str, refusal: str
) -> None:
    sync, clone, _host = synced_clone
    head = _git(clone, "rev-parse", "HEAD")
    monkeypatch.delenv("HA_API_TOKEN", raising=False)
    monkeypatch.setattr(sync.sys, "platform", "linux")
    monkeypatch.setattr(sync, "upload_to_host", lambda files: pytest.fail("uploaded despite refusal"))
    (clone / "scripts.yaml").write_text(edit)

    with pytest.raises(SystemExit, match=refusal):
        sync.do_push()

    assert _git(clone, "rev-parse", "HEAD") == head
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _git(clone, "status", "--porcelain") == "M scripts.yaml"


def test_dry_run_push_writes_nothing(synced_clone, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    sync, clone, host = synced_clone
    head = _git(clone, "rev-parse", "HEAD")
    monkeypatch.setattr(sync, "ha_api_token", lambda: pytest.fail("dry run asked for the API token"))
    monkeypatch.setattr(sync, "upload_to_host", lambda files: pytest.fail("dry run uploaded"))
    (clone / "scripts.yaml").write_text("local: edit\n")

    sync.do_push(dry_run=True)

    assert "would upload ['scripts.yaml']" in capsys.readouterr().out
    assert host["scripts.yaml"] == b"{}\n"
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _synced_commit(clone, sync) == (head, head)
    assert _git(clone, "status", "--porcelain") == "M scripts.yaml"


@pytest.mark.parametrize("gui_edit", [False, True], ids=["untouched", "gui_edit"])
def test_push_replaces_an_upload_ha_rejected(synced_clone, monkeypatch: pytest.MonkeyPatch, gui_edit: bool) -> None:
    sync, clone, host = synced_clone
    check_host_config = sync.check_host_config
    reloads = _stub_ha(monkeypatch, sync)
    monkeypatch.setattr(sync, "check_host_config", check_host_config)
    verdict = {"result": "invalid", "errors": "Invalid config for 'automation'"}
    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(json.dumps(verdict).encode()))
    (clone / "automations.yaml").write_text("- id: broken\n")
    with pytest.raises(SystemExit, match="Invalid config"):
        sync.do_push()
    assert host["automations.yaml"] == b"- id: a\n"
    # The rejected commit awaits a push, so a pull cannot bury the fix.
    with pytest.raises(SystemExit, match="origin changes await push"):
        sync.do_pull()

    if gui_edit:
        host["automations.yaml"] = b"- id: gui\n"
    (clone / "automations.yaml").write_text("- id: fixed\n")
    verdict = {"result": "valid", "errors": None}

    if gui_edit:
        with pytest.raises(SystemExit, match="host diverged"):
            sync.do_push()
        assert host["automations.yaml"] == b"- id: gui\n"
        return
    sync.do_push()

    head = _git(clone, "rev-parse", "HEAD")
    assert host["automations.yaml"] == b"- id: fixed\n"
    assert reloads == ["automation.reload"]
    assert _synced_commit(clone, sync) == (head, head)
    assert sync.resolve_ref(sync.UPLOAD_REF) is None


@pytest.mark.parametrize("rel", ["scripts.yaml", "blueprints/automation/notify.yaml"], ids=["literal", "globbed"])
def test_push_refuses_a_deletion_lab_still_holds(synced_clone, monkeypatch: pytest.MonkeyPatch, rel: str) -> None:
    sync, clone, host = synced_clone
    monkeypatch.setattr(
        sync, "SYNC_SPEC", [*sync.SYNC_SPEC, ("blueprints/automation/*", "automation.reload", sync.Direction.BOTH)]
    )
    blueprint = clone / "blueprints/automation/notify.yaml"
    blueprint.parent.mkdir(parents=True)
    blueprint.write_text("blueprint: {}\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "blueprint")
    _git(clone, "push", "--quiet", "origin", "main")
    _git(clone, "tag", "-f", sync.SYNCED_TAG)
    _git(clone, "push", "--quiet", "--force", "origin", sync.SYNCED_TAG)
    host["blueprints/automation/notify.yaml"] = blueprint.read_bytes()
    head = _git(clone, "rev-parse", "HEAD")
    _stub_ha(monkeypatch, sync)
    (clone / rel).unlink()

    with pytest.raises(SystemExit, match=f"cannot delete files from lab.*\n  {re.escape(rel)}\n"):
        sync.do_push()
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _synced_commit(clone, sync) == (head, head)

    # Once removed from lab by hand, the deletion is published and tagged.
    del host[rel]
    sync.do_push()

    pushed = _git(clone, "rev-parse", "HEAD")
    assert pushed != head
    assert rel not in _git(clone, "ls-tree", "-r", "--name-only", pushed).splitlines()
    assert _synced_commit(clone, sync) == (pushed, pushed)


def test_push_aborts_on_a_failed_host_read(synced_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, clone, _host = synced_clone
    head = _git(clone, "rev-parse", "HEAD")
    # The real reader, from a fresh load: the fixture replaced this module's copy.
    monkeypatch.setattr(
        sync, "host_file", load_repo_module("mise-tasks/ha/sync.py", name="ha_sync_read_fail").host_file
    )
    run = subprocess.run

    def ssh_drops(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[0] == "ssh":
            return subprocess.CompletedProcess(cmd, 255, b"", b"Connection reset by peer\n")
        return run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", ssh_drops)
    monkeypatch.setattr(sync, "upload_to_host", lambda files: pytest.fail("uploaded despite a failed host read"))
    (clone / "scripts.yaml").write_text("local: edit\n")

    with pytest.raises(SystemExit, match="Connection reset by peer"):
        sync.do_push()

    assert _git(clone, "rev-parse", "HEAD") == head
    assert _git(clone, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert _synced_commit(clone, sync) == (head, head)
    assert _git(clone, "status", "--porcelain") == "M scripts.yaml"


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
