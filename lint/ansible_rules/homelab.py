"""Homelab-specific ansible-lint rules."""

import functools
import re
from collections.abc import Iterator
from pathlib import Path

import yaml
from ansiblelint.errors import MatchError
from ansiblelint.file_utils import Lintable
from ansiblelint.rules import AnsibleLintRule
from ansiblelint.utils import Task, get_cmd_args

_FILE_WRITE_MODULES = {
    "copy",
    "template",
    "replace",
    "lineinfile",
    "blockinfile",
    "assemble",
    "ini_file",
}
_CONFIG_DEST_RE = re.compile(
    r"(^/etc/|/\.config/|"
    r"\.(?:conf|cfg|ini|json|rules|service|timer|toml|yaml|yml)$|"
    r"/(?:config|config\.yaml|config\.yml|env|environment)$)"
)


def _module_name(task: Task) -> str:
    return task["action"]["__ansible_module__"].rsplit(".", 1)[-1]


def _is_test_hook(file: Lintable | None) -> bool:
    return file is not None and file.path.name.startswith(("_verify", "_setup"))


def _is_test_playbook(file: Lintable | None) -> bool:
    return file is not None and file.path.full_match("**/test/playbooks/**")


def _role_fixture_has_named_entrypoints(file: Lintable | None, role_name: object) -> bool:
    if file is None or not isinstance(role_name, str) or "{{" in role_name:
        return False
    path = file.path
    if not _is_test_hook(file) or path.parent.name != "tasks" or path.parent.parent.parent.name != "roles":
        return False
    tasks_dir = path.parent.parent.parent / role_name / "tasks"
    if not tasks_dir.is_dir():
        return False
    return any(
        task_file.is_file()
        and task_file.suffix in {".yml", ".yaml"}
        and task_file.stem != "main"
        and not task_file.stem.startswith("_")
        for task_file in tasks_dir.iterdir()
    )


def _is_test_file(file: Lintable | None) -> bool:
    return _is_test_hook(file) or _is_test_playbook(file)


class _HomelabRule(AnsibleLintRule):
    version_changed = "1.0.0"


class RequireBackup(_HomelabRule):
    """File-writing tasks must set `backup: true`."""

    id = "require-backup"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
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


class RequireNamedRoleEntrypoint(_HomelabRule):
    """Static test fixtures must select the role task file they depend on."""

    id = "require-named-role-entrypoint"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _module_name(task) != "import_role":
            return False

        if task["action"].get("tasks_from"):
            return False
        central_fixture = _is_test_playbook(file) and file is not None and file.path.name != "site.yml"
        role_fixture = _role_fixture_has_named_entrypoints(file, task["action"].get("name"))
        if not central_fixture and not role_fixture:
            return False
        return "static test fixture role imports must set `tasks_from:`"


class ShellStrictMode(_HomelabRule):
    """Shell tasks must start with `set -euo pipefail` under /bin/bash."""

    id = "shell-strict-mode"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _module_name(task) != "shell" or _is_test_hook(file):
            return False

        cmd = self.unjinja(get_cmd_args(task))
        executable = task["action"].get("executable") or ""

        missing = []
        preamble = "set -euo pipefail"
        if cmd != preamble and not cmd.startswith((f"{preamble}\n", f"{preamble};")):
            missing.append(preamble)
        if not executable.endswith("bash"):
            missing.append("executable: /bin/bash")

        return False if not missing else f"shell task missing {', '.join(missing)}"


class NoHandlers(_HomelabRule):
    """Service restarts must be driven inline instead of through handlers."""

    id = "no-handlers"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        if task.is_handler() or "notify" in task.raw_task:
            return "handlers are banned; drive restarts inline from *_result.changed"
        return False


class NoNoLog(_HomelabRule):
    """Tasks must not hide diffs or failure output with `no_log: true`."""

    id = "no-no-log"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        no_log = task.raw_task.get("no_log")
        if no_log is True or (isinstance(no_log, str) and no_log.lower() == "true"):
            return "`no_log: true` is banned in this repo; keep failures inspectable"
        return False


class NoInventoryHostnameWhen(_HomelabRule):
    """Task branching must use host vars, not hard-coded inventory names."""

    id = "no-inventory-hostname-when"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        when = str(task.raw_task.get("when") or "")
        if "inventory_hostname" in when:
            return "task `when:` branches must use host vars instead of inventory_hostname"
        return False


class PreferImport(_HomelabRule):
    """Prefer static imports unless the include is genuinely dynamic."""

    id = "prefer-import"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        module = _module_name(task)
        if module not in {"include_role", "include_tasks"}:
            return False

        if "loop" in task.raw_task or any(str(key).startswith("with_") for key in task.raw_task):
            return False

        action = task["action"]
        include_target = str(action.get("_raw_params") or action.get("file") or action.get("name") or "")
        if "{{" in include_target or "}}" in include_target:
            return False

        return f"use import_{module.removeprefix('include_')} unless this include needs runtime evaluation"


class RequireValidate(_HomelabRule):
    """Config-writing copy/template tasks should parse-test rendered content."""

    id = "require-validate"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if _is_test_file(file):
            return False

        module = _module_name(task)
        if module not in {"copy", "template"}:
            return False

        action = task["action"]
        if "validate" in action:
            return False

        destination = str(action.get("dest") or action.get("path") or "")
        if destination and _CONFIG_DEST_RE.search(destination):
            return f"{module} task writes config-like content without `validate:`"
        return False


_BLOCK_KEYS = ("block", "rescue", "always")
_IMPORT_MODULES = {"import_role", "include_role", "import_tasks", "include_tasks"}


class _IgnoreTagsLoader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's custom tags (!vault, !unsafe) as null."""


_IgnoreTagsLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _short_module(key: object) -> str:
    return str(key).rsplit(".", 1)[-1]


def _task_tags(task: dict) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else {str(tag) for tag in tags}


def _walk_tasks(tasks: object) -> Iterator[dict]:
    """Yield every task dict, descending into block/rescue/always."""
    for task in tasks if isinstance(tasks, list) else []:
        if isinstance(task, dict):
            yield task
            for key in _BLOCK_KEYS:
                yield from _walk_tasks(task.get(key))


@functools.cache
def _caller_tagged(roles_dir: Path) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """Helper roles, and (role, task file stem) entry points other roles call.

    Helper roles keep an operationally empty tasks/main.yml; a task file another
    role calls through import_role/include_role `tasks_from` is a helper entry
    point. In both cases the caller's import carries the tag scope.
    """
    helpers: set[str] = set()
    entrypoints: set[tuple[str, str]] = set()
    for tasks_file in roles_dir.glob("*/tasks/*.yml"):
        role = tasks_file.parent.parent.name
        tasks = yaml.load(tasks_file.read_text(), Loader=_IgnoreTagsLoader)
        if tasks_file.stem == "main" and not tasks:
            helpers.add(role)
        for task in _walk_tasks(tasks):
            for key, args in task.items():
                if _short_module(key) in {"import_role", "include_role"} and isinstance(args, dict):
                    name, tasks_from = args.get("name"), args.get("tasks_from")
                    if isinstance(name, str) and tasks_from and name != role:
                        entrypoints.add((name, Path(str(tasks_from)).stem))
    return frozenset(helpers), frozenset(entrypoints)


class RequireRoleTag(_HomelabRule):
    """Every task in a role carries the role name as a tag."""

    id = "require-role-tag"

    def matchyaml(self, file: Lintable) -> list[MatchError]:
        path = file.path.resolve()
        # `_`-prefixed task files (_setup, _verify, test stubs) are harness scaffolding.
        if file.kind != "tasks" or path.name.startswith("_") or path.parent.name != "tasks":
            return []
        roles_dir = path.parent.parent.parent
        role = path.parent.parent.name
        if roles_dir.name != "roles":
            return []
        helpers, entrypoints = _caller_tagged(roles_dir)
        if role in helpers or (role, path.stem) in entrypoints:
            return []
        return [
            self.create_matcherror(message=f"task is missing the `{role}` role tag", filename=file, data=task)
            for task in _untagged_tasks(file.data, role, inherited=False)
        ]


def _untagged_tasks(tasks: object, role: str, *, inherited: bool) -> Iterator[dict]:
    """Yield leaf tasks lacking `role` in their own or an enclosing block's tags.

    Import/include tasks are skipped: the files they pull in are checked
    on their own.
    """
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        tagged = inherited or role in _task_tags(task)
        if any(key in task for key in _BLOCK_KEYS):
            for key in _BLOCK_KEYS:
                yield from _untagged_tasks(task.get(key), role, inherited=tagged)
        elif not tagged and not any(_short_module(key) in _IMPORT_MODULES for key in task):
            yield task
