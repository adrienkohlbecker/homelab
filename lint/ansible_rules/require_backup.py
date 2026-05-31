"""Custom ansible-lint rule: file-writing tasks must set `backup: true`.

Enforces the CLAUDE.md convention that every task which writes or edits a file
on disk also makes a timestamped backup, for on-disk traceability of what
changed and when.

Two reasons this is safe to mandate universally rather than case-by-case:
  * No credential exposure. ansible's `preserved_copy` (used by `backup: true`)
    copies the original's mode via copy2/copystat AND chowns to its uid/gid, so
    a backup of a 0600 secret-bearing file stays 0600 with the same owner — it
    never widens access.
  * No disk bloat. The cleanup role prunes backups older than a week on a daily
    timer (roles/cleanup `prune_ansible_backups`), so they don't accumulate.

Scope: production tasks only. Test scaffolding (`_verify*.yml`, `_setup*.yml`)
is exempt — the fixtures it writes are throwaway and don't warrant backups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ansiblelint.rules import AnsibleLintRule

if TYPE_CHECKING:
    from ansiblelint.file_utils import Lintable
    from ansiblelint.utils import Task

# Modules that write/edit a file on disk AND accept a `backup:` parameter. The
# `file` module is intentionally absent: it has no `backup:` (its writes are
# directories/symlinks/touch/absent, nothing to back up).
_FILE_WRITE_MODULES = frozenset(
    {
        "copy",
        "template",
        "replace",
        "lineinfile",
        "blockinfile",
        "assemble",
        "ini_file",
    }
)


class RequireBackup(AnsibleLintRule):
    """File-writing tasks must set `backup: true`."""

    id = "require-backup"
    description = (
        "Every task that writes or edits a file (copy, template, replace, "
        "lineinfile, blockinfile, assemble, ini_file) must set `backup: true` "
        "for on-disk traceability. Backups inherit the original's mode and "
        "ownership (so a secret-bearing file's backup is never widened) and the "
        "cleanup role prunes them daily, so there is no security or disk-bloat "
        "reason to skip it. Test scaffolding (`_verify*`/`_setup*`) is exempt."
    )
    severity = "MEDIUM"
    tags = ["idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task":
            return False

        # Match the module's short name so community.general.ini_file and a bare
        # ini_file both resolve to "ini_file".
        module = task["action"]["__ansible_module__"].rsplit(".", 1)[-1]
        if module not in _FILE_WRITE_MODULES:
            return False

        if file is not None:
            # Exempt test-only hooks: _verify.yml, _verify_*.yml, _setup.yml.
            if file.path.name.startswith(("_verify", "_setup")):
                return False
            # Exempt test-fixture roles (roles/test, roles/packer) — their
            # writes target throwaway VMs, same rationale as _verify/_setup.
            parts = file.path.parts
            if "roles" in parts:
                role = parts.index("roles") + 1
                if role < len(parts) and parts[role] in [ "test", "packer" ]:
                    return False

        backup = task["action"].get("backup", None)
        if backup is True:
            return False
        # A templated value (e.g. `backup: "{{ ... }}"`) can't be evaluated
        # statically — accept it rather than emit a false positive.
        if isinstance(backup, str):
            return False
        if backup is False:
            return f"{module} task sets `backup: false`; config writes must keep backups"
        return f"{module} task is missing `backup: true`"
