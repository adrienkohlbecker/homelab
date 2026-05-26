#!/usr/bin/env -S uv run --script
"""Inverse-dependency lookup for helper roles.

Walks roles/*/tasks/*.yml, parses YAML, follows block/rescue/always nesting,
and collects import_role / include_role references. Prints (one per line) the
roles that consume the given helper.

Used by ci:detect-roles to expand a helper-role change into its set of
consumer roles. A naive grep would miss `import_role` calls inside `block:`
sections (nginx and others use blocks heavily); the recursive YAML walk is
robust against that.
"""

import sys
from collections import defaultdict
from pathlib import Path

import yaml


def walk(tasks: object, role: str, inv: dict[str, set[str]]) -> None:
    """Recurse a task list, collecting import/include_role references.

    Tasks can nest under block/rescue/always; the walker descends through all
    three. Anything that isn't a dict is silently skipped (string-form
    `import_tasks: foo.yml` shorthand, free-form comments-as-strings, etc.).
    """
    if not isinstance(tasks, list):
        return
    for t in tasks:
        if not isinstance(t, dict):
            continue
        for k in ("import_role", "include_role"):
            body = t.get(k)
            if isinstance(body, dict) and "name" in body:
                inv[body["name"]].add(role)
        for nest in ("block", "rescue", "always"):
            if nest in t:
                walk(t[nest], role, inv)


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) != 1:
        print("usage: ci:role-deps <helper-role-name>", file=sys.stderr)
        return 2
    helper = argv[0]

    inv: dict[str, set[str]] = defaultdict(set)
    for task_file in sorted(Path("roles").glob("*/tasks/*.yml")):
        role = task_file.parts[-3]
        try:
            with task_file.open() as fh:
                tasks = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            print(f"warning: failed to parse {task_file}: {e}", file=sys.stderr)
            continue
        walk(tasks, role, inv)

    for consumer in sorted(inv.get(helper, set())):
        print(consumer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
