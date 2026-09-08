"""Unit tests for custom ansible-lint rules."""

from pathlib import Path

import pytest
from ansiblelint.file_utils import Lintable
from ansiblelint.utils import Task

from lint.ansible_rules.homelab import (
    NoHandlers,
    NoInventoryHostnameWhen,
    NoNoLog,
    PreferImport,
    RequireBackup,
    RequireNamedRoleEntrypoint,
    RequireValidate,
    ShellStrictMode,
)

_ROOT = Path(__file__).resolve().parent.parent


def _task(module: str, module_args=None, *, kind: str = "tasks", **task_fields) -> Task:
    return Task({module: {} if module_args is None else module_args, **task_fields}, kind=kind)


class TestShellStrictMode:
    @pytest.mark.parametrize(
        ("cmd", "missing"),
        [
            ("set -euo pipefail\necho hello", []),
            ("set -euo pipefail; echo hello", []),
            ("echo hello", ["set -euo pipefail"]),
            ("echo before\nset -euo pipefail", ["set -euo pipefail"]),
            ("set -euo pipefailure", ["set -euo pipefail"]),
        ],
    )
    def test_strict_mode_requirements(self, cmd: str, missing: list[str]) -> None:
        result = ShellStrictMode().matchtask(_task("shell", cmd, args={"executable": "/bin/bash"}))
        expected = False if not missing else f"shell task missing {', '.join(missing)}"
        assert result == expected

    def test_requires_bash(self) -> None:
        result = ShellStrictMode().matchtask(
            _task("shell", "set -euo pipefail\necho hello", args={"executable": "/bin/sh"})
        )
        assert result == "shell task missing executable: /bin/bash"

    def test_test_hooks_are_exempt(self) -> None:
        result = ShellStrictMode().matchtask(_task("shell", "echo hello"), Lintable("roles/x/tasks/_verify.yml"))
        assert result is False


class TestRequireBackup:
    @pytest.mark.parametrize(
        "module",
        ["copy", "template", "replace", "lineinfile", "blockinfile", "assemble", "ini_file"],
    )
    def test_requires_backup(self, module: str) -> None:
        assert RequireBackup().matchtask(_task(module)) == f"{module} task is missing `backup: true`"

    def test_accepts_fqcn_and_templated_backup(self) -> None:
        assert RequireBackup().matchtask(_task("community.general.ini_file", {"backup": "{{ keep_backup }}"})) is False

    def test_flags_backup_false(self) -> None:
        assert (
            RequireBackup().matchtask(_task("copy", {"backup": False}))
            == "copy task sets `backup: false`; config writes must keep backups"
        )

    def test_ignores_non_file_write_modules(self) -> None:
        assert RequireBackup().matchtask(_task("file")) is False

    @pytest.mark.parametrize(
        "path",
        [
            "roles/x/tasks/_setup.yml",
            "test/playbooks/_environment.yml",
            "/repo/test/playbooks/_environment.yml",
        ],
    )
    def test_test_files_are_exempt(self, path: str) -> None:
        assert RequireBackup().matchtask(_task("copy"), Lintable(path)) is False


class TestRequireNamedRoleEntrypoint:
    def test_static_fixture_requires_tasks_from(self) -> None:
        result = RequireNamedRoleEntrypoint().matchtask(
            _task("import_role", {"name": "apt"}),
            Lintable("test/playbooks/build_box_deps.yml"),
        )
        assert result == "static test fixture role imports must set `tasks_from:`"

    def test_named_entrypoint_is_allowed(self) -> None:
        result = RequireNamedRoleEntrypoint().matchtask(
            _task("ansible.builtin.import_role", {"name": "apt", "tasks_from": "configure"}),
            Lintable("/repo/test/playbooks/_environment.yml"),
        )
        assert result is False

    def test_role_fixture_requires_tasks_from_for_role_with_named_entrypoints(self, tmp_path: Path) -> None:
        dependency_tasks = tmp_path / "roles" / "dependency" / "tasks"
        dependency_tasks.mkdir(parents=True)
        (dependency_tasks / "main.yml").write_text("---\n")
        (dependency_tasks / "configure.yml").write_text("---\n")
        fixture = tmp_path / "roles" / "consumer" / "tasks" / "_setup.yml"

        result = RequireNamedRoleEntrypoint().matchtask(
            _task("import_role", {"name": "dependency"}),
            Lintable(fixture),
        )

        assert result == "static test fixture role imports must set `tasks_from:`"

    def test_role_fixture_allows_main_only_role(self, tmp_path: Path) -> None:
        dependency_tasks = tmp_path / "roles" / "dependency" / "tasks"
        dependency_tasks.mkdir(parents=True)
        (dependency_tasks / "main.yml").write_text("---\n")
        fixture = tmp_path / "roles" / "consumer" / "tasks" / "_verify.yml"

        result = RequireNamedRoleEntrypoint().matchtask(
            _task("import_role", {"name": "dependency"}),
            Lintable(fixture),
        )

        assert result is False

    @pytest.mark.parametrize(
        "path",
        ["test/playbooks/site.yml", "roles/example/tasks/main.yml"],
    )
    def test_normal_role_entrypoints_are_allowed(self, path: str) -> None:
        result = RequireNamedRoleEntrypoint().matchtask(_task("import_role", {"name": "example"}), Lintable(path))
        assert result is False


class TestNoHandlers:
    def test_notify_is_banned(self) -> None:
        result = NoHandlers().matchtask(_task("template", notify="Restart service"))
        assert result == "handlers are banned; drive restarts inline from *_result.changed"

    def test_handler_task_is_banned(self) -> None:
        result = NoHandlers().matchtask(_task("systemd", kind="handlers"))

        assert result == "handlers are banned; drive restarts inline from *_result.changed"

    def test_test_files_are_exempt(self) -> None:
        result = NoHandlers().matchtask(
            _task("template", notify="Restart service"), Lintable("roles/x/tasks/_verify.yml")
        )
        assert result is False


class TestNoNoLog:
    @pytest.mark.parametrize("no_log", [True, "true", "True"])
    def test_no_log_true_is_banned(self, no_log) -> None:
        result = NoNoLog().matchtask(_task("command", no_log=no_log))
        assert result == "`no_log: true` is banned in this repo; keep failures inspectable"

    def test_no_log_false_is_allowed(self) -> None:
        assert NoNoLog().matchtask(_task("command", no_log=False)) is False


class TestNoInventoryHostnameWhen:
    def test_inventory_hostname_in_when_is_banned(self) -> None:
        result = NoInventoryHostnameWhen().matchtask(_task("debug", when="inventory_hostname in ['lab', 'pug']"))
        assert result == "task `when:` branches must use host vars instead of inventory_hostname"

    def test_host_var_when_is_allowed(self) -> None:
        result = NoInventoryHostnameWhen().matchtask(_task("debug", when="foo_enabled | default(false)"))
        assert result is False


class TestPreferImport:
    def test_static_include_tasks_warns(self) -> None:
        result = PreferImport().matchtask(_task("include_tasks", "service.yml"))
        assert result == "use import_tasks unless this include needs runtime evaluation"

    def test_reset_connection_include_tasks_warns(self) -> None:
        result = PreferImport().matchtask(_task("include_tasks", "reset_connection.yml"))
        assert result == "use import_tasks unless this include needs runtime evaluation"

    def test_loop_include_tasks_is_allowed(self) -> None:
        result = PreferImport().matchtask(_task("include_tasks", "service.yml", loop=[1, 2]))
        assert result is False

    def test_templated_include_role_is_allowed(self) -> None:
        result = PreferImport().matchtask(_task("include_role", {"name": "{{ role_name }}"}))
        assert result is False


class TestRequireValidate:
    def test_config_template_without_validate_warns(self) -> None:
        result = RequireValidate().matchtask(_task("template", {"dest": "/etc/example.conf"}))
        assert result == "template task writes config-like content without `validate:`"

    def test_validate_is_allowed(self) -> None:
        result = RequireValidate().matchtask(
            _task("template", {"dest": "/etc/example.conf", "validate": "nginx -t -c %s"})
        )
        assert result is False

    def test_non_config_destination_is_allowed(self) -> None:
        assert RequireValidate().matchtask(_task("copy", {"dest": "/mnt/services/foo/data.txt"})) is False
