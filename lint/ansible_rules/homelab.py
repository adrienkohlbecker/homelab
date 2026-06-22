"""Homelab-specific ansible-lint rules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from ansiblelint.rules import AnsibleLintRule
from ansiblelint.utils import get_cmd_args

if TYPE_CHECKING:
    from ansiblelint.file_utils import Lintable
    from ansiblelint.utils import Task

_FILE_WRITE_MODULES = {
    "copy",
    "template",
    "replace",
    "lineinfile",
    "blockinfile",
    "assemble",
    "ini_file",
}
_TEST_HOOK_PREFIXES = ("_verify", "_setup")
_TEST_FIXTURE_ROLES = {"test", "packer"}
_INCLUDE_MODULES = {"include_role", "include_tasks"}
_CONFIG_WRITE_MODULES = {"copy", "template"}
_CONFIG_DEST_RE = re.compile(
    r"(^/etc/|/\.config/|"
    r"\.(?:conf|cfg|ini|json|rules|service|timer|toml|yaml|yml)$|"
    r"/(?:config|config\.yaml|config\.yml|env|environment)$)"
)

_SET_TOKEN_RE = r"(?<![\w-])set(?![\w-])"
_SET_E_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n;]*-[A-Za-z]*e", re.MULTILINE)
_SET_U_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n;]*-[A-Za-z]*u", re.MULTILINE)
_PIPEFAIL_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n]*pipefail", re.MULTILINE)


def _module_name(task: Task) -> str:
    return task["action"]["__ansible_module__"].rsplit(".", 1)[-1]


def _raw_task(task: Task) -> dict:
    return task.get("__raw_task__", task)  # type: ignore[return-value]


def _is_test_hook(file: Lintable | None) -> bool:
    return file is not None and file.path.name.startswith(_TEST_HOOK_PREFIXES)


def _is_test_file(file: Lintable | None) -> bool:
    if file is None:
        return False
    if _is_test_hook(file):
        return True
    if "roles" not in file.path.parts:
        return False
    parts = file.path.parts
    role = parts.index("roles") + 1
    return role < len(parts) and parts[role] in _TEST_FIXTURE_ROLES


def _stringify(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_stringify(item)}" for key, item in value.items())
    return "" if value is None else str(value)


class RequireBackup(AnsibleLintRule):
    """File-writing tasks must set `backup: true`."""

    id = "require-backup"
    severity = "MEDIUM"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task":
            return False

        module = _module_name(task)
        if module not in _FILE_WRITE_MODULES:
            return False

        if _is_test_file(file):
            return False

        backup = task["action"].get("backup")
        if backup is True or isinstance(backup, str):
            return False
        if backup is False:
            return f"{module} task sets `backup: false`; config writes must keep backups"
        return f"{module} task is missing `backup: true`"


class ShellStrictMode(AnsibleLintRule):
    """Shell tasks must start with `set -euo pipefail` under /bin/bash."""

    id = "shell-strict-mode"
    severity = "MEDIUM"
    tags: ClassVar[list[str]] = ["command-shell", "idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task" or _module_name(task) != "shell" or _is_test_hook(file):
            return False

        cmd = self.unjinja(get_cmd_args(task))
        executable = task["action"].get("executable") or ""

        missing = []
        if not _SET_E_RE.search(cmd):
            missing.append("set -e")
        if not _SET_U_RE.search(cmd):
            missing.append("set -u")
        if not _PIPEFAIL_RE.search(cmd):
            missing.append("set -o pipefail")
        if not executable.endswith("bash"):
            missing.append("executable: /bin/bash")

        return False if not missing else f"shell task missing {', '.join(missing)}"


class NoHandlers(AnsibleLintRule):
    """Service restarts must be driven inline instead of through handlers."""

    id = "no-handlers"
    severity = "HIGH"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        action_type = task["__ansible_action_type__"]
        if action_type == "handler" or (file is not None and "handlers" in file.path.parts):
            return "handlers are banned; drive restarts inline from *_result.changed"
        if action_type != "task":
            return False

        raw_task = _raw_task(task)
        if "notify" in raw_task:
            return "handlers are banned; drive restarts inline from *_result.changed"
        return False


class NoNoLog(AnsibleLintRule):
    """Tasks must not hide diffs or failure output with `no_log: true`."""

    id = "no-no-log"
    severity = "MEDIUM"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task" or _is_test_file(file):
            return False

        no_log = _raw_task(task).get("no_log")
        if no_log is True or (isinstance(no_log, str) and no_log.lower() == "true"):
            return "`no_log: true` is banned in this repo; keep failures inspectable"
        return False


class NoInventoryHostnameWhen(AnsibleLintRule):
    """Task branching must use host vars, not hard-coded inventory names."""

    id = "no-inventory-hostname-when"
    severity = "MEDIUM"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task" or _is_test_file(file):
            return False

        when = _stringify(_raw_task(task).get("when"))
        if "inventory_hostname" in when:
            return "task `when:` branches must use host vars instead of inventory_hostname"
        return False


class PreferImport(AnsibleLintRule):
    """Prefer static imports unless the include is genuinely dynamic."""

    id = "prefer-import"
    severity = "LOW"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task" or _is_test_file(file):
            return False

        module = _module_name(task)
        if module not in _INCLUDE_MODULES:
            return False

        raw_task = _raw_task(task)
        if "loop" in raw_task or any(str(key).startswith("with_") for key in raw_task):
            return False

        action = task["action"]
        include_target = _stringify(action.get("_raw_params") or action.get("file") or action.get("name"))
        if "{{" in include_target or "}}" in include_target:
            return False
        if module == "include_tasks" and "reset_connection" in include_target:
            return False

        return f"use import_{module.removeprefix('include_')} unless this include needs runtime evaluation"


class RequireValidate(AnsibleLintRule):
    """Config-writing copy/template tasks should parse-test rendered content."""

    id = "require-validate"
    severity = "LOW"
    tags: ClassVar[list[str]] = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task" or _is_test_file(file):
            return False

        module = _module_name(task)
        if module not in _CONFIG_WRITE_MODULES:
            return False

        action = task["action"]
        if "validate" in action:
            return False

        destination = _stringify(action.get("dest") or action.get("path"))
        if destination and _CONFIG_DEST_RE.search(destination):
            return f"{module} task writes config-like content without `validate:`"
        return False
