"""Homelab-specific ansible-lint rules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

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

_SET_TOKEN_RE = r"(?<![\w-])set(?![\w-])"
_SET_E_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n;]*-[A-Za-z]*e", re.MULTILINE)
_SET_U_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n;]*-[A-Za-z]*u", re.MULTILINE)
_PIPEFAIL_RE = re.compile(rf"{_SET_TOKEN_RE}[^\n]*pipefail", re.MULTILINE)


def _module_name(task: Task) -> str:
    return task["action"]["__ansible_module__"].rsplit(".", 1)[-1]


def _is_test_hook(file: Lintable | None) -> bool:
    return file is not None and file.path.name.startswith(_TEST_HOOK_PREFIXES)


class RequireBackup(AnsibleLintRule):
    """File-writing tasks must set `backup: true`."""

    id = "require-backup"
    description = "File-writing tasks must keep backups; test hooks and fixture roles are exempt."
    severity = "MEDIUM"
    tags = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task":
            return False

        module = _module_name(task)
        if module not in _FILE_WRITE_MODULES:
            return False

        if _is_test_hook(file):
            return False
        if file is not None and "roles" in file.path.parts:
            parts = file.path.parts
            role = parts.index("roles") + 1
            if role < len(parts) and parts[role] in _TEST_FIXTURE_ROLES:
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
    description = "Production shell tasks must use strict bash; test hooks are exempt."
    severity = "MEDIUM"
    tags = ["command-shell", "idiom"]
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
