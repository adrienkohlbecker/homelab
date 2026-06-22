"""Unit tests for custom ansible-lint rules."""

import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_RULES = runpy.run_path(str(_ROOT / "lint" / "ansible_rules" / "homelab.py"))
NoHandlers = _RULES["NoHandlers"]
NoInventoryHostnameWhen = _RULES["NoInventoryHostnameWhen"]
NoNoLog = _RULES["NoNoLog"]
PreferImport = _RULES["PreferImport"]
RequireBackup = _RULES["RequireBackup"]
RequireValidate = _RULES["RequireValidate"]
ShellStrictMode = _RULES["ShellStrictMode"]


def _task(module: str, **action):
    raw_task = action.pop("__raw_task__", None)
    task = {"__ansible_action_type__": "task", "action": {"__ansible_module__": module, **action}}
    if raw_task is not None:
        task["__raw_task__"] = raw_task
    return task


def _lintable(path: str):
    return SimpleNamespace(path=Path(path))


class TestShellStrictMode:
    @pytest.mark.parametrize(
        ("cmd", "missing"),
        [
            ("set -euo pipefail\necho hello", []),
            ("set -e\nset -u\nset -o pipefail\necho hello", []),
            ("set -uo pipefail\necho hello", ["set -e"]),
            ("set -eo pipefail\necho hello", ["set -u"]),
            ("set -eu\necho hello", ["set -o pipefail"]),
            ("echo hello", ["set -e", "set -u", "set -o pipefail"]),
            ("unset -e FOO", ["set -e", "set -u", "set -o pipefail"]),
            ("reset-property -e something", ["set -e", "set -u", "set -o pipefail"]),
            ("set -euo pipefail; echo hello", []),
        ],
    )
    def test_strict_mode_requirements(self, cmd: str, missing: list[str]) -> None:
        result = ShellStrictMode().matchtask(_task("shell", _raw_params=cmd, executable="/bin/bash"))
        expected = False if not missing else f"shell task missing {', '.join(missing)}"
        assert result == expected

    def test_requires_bash(self) -> None:
        result = ShellStrictMode().matchtask(
            _task("shell", _raw_params="set -euo pipefail\necho hello", executable="/bin/sh")
        )
        assert result == "shell task missing executable: /bin/bash"

    def test_test_hooks_are_exempt(self) -> None:
        result = ShellStrictMode().matchtask(
            _task("shell", _raw_params="echo hello"), _lintable("roles/x/tasks/_verify.yml")
        )
        assert result is False


class TestRequireBackup:
    @pytest.mark.parametrize(
        "module",
        ["copy", "template", "replace", "lineinfile", "blockinfile", "assemble", "ini_file"],
    )
    def test_requires_backup(self, module: str) -> None:
        assert RequireBackup().matchtask(_task(module)) == f"{module} task is missing `backup: true`"

    def test_accepts_fqcn_and_templated_backup(self) -> None:
        assert RequireBackup().matchtask(_task("community.general.ini_file", backup="{{ keep_backup }}")) is False

    def test_flags_backup_false(self) -> None:
        assert (
            RequireBackup().matchtask(_task("copy", backup=False))
            == "copy task sets `backup: false`; config writes must keep backups"
        )

    def test_ignores_non_file_write_modules(self) -> None:
        assert RequireBackup().matchtask(_task("file")) is False

    @pytest.mark.parametrize(
        "path",
        ["roles/x/tasks/_setup.yml", "roles/test/tasks/main.yml", "roles/packer/tasks/main.yml"],
    )
    def test_test_files_are_exempt(self, path: str) -> None:
        assert RequireBackup().matchtask(_task("copy"), _lintable(path)) is False


class TestNoHandlers:
    def test_notify_is_banned(self) -> None:
        result = NoHandlers().matchtask(_task("template", __raw_task__={"notify": "Restart service"}))
        assert result == "handlers are banned; drive restarts inline from *_result.changed"

    def test_handler_task_is_banned(self) -> None:
        task = _task("systemd")
        task["__ansible_action_type__"] = "handler"

        result = NoHandlers().matchtask(task)

        assert result == "handlers are banned; drive restarts inline from *_result.changed"

    def test_test_files_are_exempt(self) -> None:
        result = NoHandlers().matchtask(
            _task("template", __raw_task__={"notify": "Restart service"}), _lintable("roles/x/tasks/_verify.yml")
        )
        assert result is False


class TestNoNoLog:
    @pytest.mark.parametrize("no_log", [True, "true", "True"])
    def test_no_log_true_is_banned(self, no_log) -> None:
        result = NoNoLog().matchtask(_task("command", __raw_task__={"no_log": no_log}))
        assert result == "`no_log: true` is banned in this repo; keep failures inspectable"

    def test_no_log_false_is_allowed(self) -> None:
        assert NoNoLog().matchtask(_task("command", __raw_task__={"no_log": False})) is False


class TestNoInventoryHostnameWhen:
    def test_inventory_hostname_in_when_is_banned(self) -> None:
        result = NoInventoryHostnameWhen().matchtask(
            _task("debug", __raw_task__={"when": "inventory_hostname in ['lab', 'pug']"})
        )
        assert result == "task `when:` branches must use host vars instead of inventory_hostname"

    def test_host_var_when_is_allowed(self) -> None:
        result = NoInventoryHostnameWhen().matchtask(
            _task("debug", __raw_task__={"when": "foo_enabled | default(false)"})
        )
        assert result is False


class TestPreferImport:
    def test_static_include_tasks_warns(self) -> None:
        result = PreferImport().matchtask(_task("include_tasks", _raw_params="service.yml"))
        assert result == "use import_tasks unless this include needs runtime evaluation"

    def test_loop_include_tasks_is_allowed(self) -> None:
        result = PreferImport().matchtask(
            _task("include_tasks", _raw_params="service.yml", __raw_task__={"loop": [1, 2]})
        )
        assert result is False

    def test_templated_include_role_is_allowed(self) -> None:
        result = PreferImport().matchtask(_task("include_role", name="{{ role_name }}"))
        assert result is False


class TestRequireValidate:
    def test_config_template_without_validate_warns(self) -> None:
        result = RequireValidate().matchtask(_task("template", dest="/etc/example.conf"))
        assert result == "template task writes config-like content without `validate:`"

    def test_validate_is_allowed(self) -> None:
        result = RequireValidate().matchtask(_task("template", dest="/etc/example.conf", validate="nginx -t -c %s"))
        assert result is False

    def test_non_config_destination_is_allowed(self) -> None:
        assert RequireValidate().matchtask(_task("copy", dest="/mnt/services/foo/data.txt")) is False


class TestTestMetaValidation:
    def test_all_meta_files_valid(self) -> None:
        """Run test-meta.py against the real repo — catches typos in machine/ubuntu."""
        result = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "mise-tasks" / "lint" / "test-meta.py"),
            ],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
            timeout=30,
        )
        assert result.returncode == 0, f"test-meta.py failed:\n{result.stderr}"
        assert "Validated" in result.stdout
