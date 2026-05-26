"""Custom ansible-lint rule: shell tasks must run under strict bash.

Enforces the CLAUDE.md convention that every `shell:` task starts with
`set -euo pipefail` and runs under `/bin/bash` (pipefail is a bashism;
ansible's default `/bin/sh` is dash, which errors on `set -o pipefail`).

`risky-shell-pipe` (built-in) only covers pipefail, and only when a pipe
is present. This rule additionally requires `-e` and `-u`, and the bash
executable.

Scope: production tasks only. Test scaffolding (`_verify*.yml`,
`_setup*.yml`) is exempt — those probes routinely assert failure paths
(non-zero rc, mid-pipeline failures) where `set -e` would be wrong.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ansiblelint.rules import AnsibleLintRule
from ansiblelint.utils import get_cmd_args

if TYPE_CHECKING:
    from ansiblelint.file_utils import Lintable
    from ansiblelint.utils import Task

# Each flag may appear in a combined `set -euo pipefail` or split across
# lines (`set -e` / `set -u`). Match per `set` statement, not across `;`.
# `(?<![\w-])set(?![\w-])` pins `set` as a standalone token so the substring
# inside another word (`unset`, `reset`, `set-property`) can't satisfy the
# check on a task that happens to carry a trailing `-...e`/`-...u` flag on the
# same segment.
_SET_E_RE = re.compile(r"(?<![\w-])set(?![\w-])[^\n;]*-[A-Za-z]*e", re.MULTILINE)
_SET_U_RE = re.compile(r"(?<![\w-])set(?![\w-])[^\n;]*-[A-Za-z]*u", re.MULTILINE)
_PIPEFAIL_RE = re.compile(r"(?<![\w-])set(?![\w-])[^\n]*pipefail", re.MULTILINE)


class ShellStrictMode(AnsibleLintRule):
    """Shell tasks must start with `set -euo pipefail` under /bin/bash."""

    id = "shell-strict-mode"
    description = (
        "Every `shell:` task must start with `set -euo pipefail` and declare "
        "`executable: /bin/bash`. `-e` aborts on the first failed command, "
        "`-u` on an unset variable, and `-o pipefail` on a failed pipe stage "
        "(a bashism, hence /bin/bash). Test scaffolding (`_verify*`/`_setup*`) "
        "is exempt because those probes deliberately exercise failure paths."
    )
    severity = "MEDIUM"
    tags = ["command-shell", "idiom"]
    version_changed = "1.0.0"

    def matchtask(self, task: Task, file: Lintable | None = None) -> bool | str:
        if task["__ansible_action_type__"] != "task":
            return False
        if task["action"]["__ansible_module__"] != "shell":
            return False

        # Exempt test-only hooks: _verify.yml, _verify_*.yml, _setup.yml.
        if file is not None and file.path.name.startswith(("_verify", "_setup")):
            return False

        cmd = self.unjinja(get_cmd_args(task))
        executable = task["action"].get("executable", "") or ""

        missing = []
        if not _SET_E_RE.search(cmd):
            missing.append("set -e")
        if not _SET_U_RE.search(cmd):
            missing.append("set -u")
        if not _PIPEFAIL_RE.search(cmd):
            missing.append("set -o pipefail")
        if not executable.endswith("bash"):
            missing.append("executable: /bin/bash")

        if not missing:
            return False
        return f"shell task missing {', '.join(missing)}"
