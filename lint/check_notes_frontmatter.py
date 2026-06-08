#!/usr/bin/env python3
"""Validate that every notes/*.md has required YAML frontmatter fields.

Required fields:
  status:     one of VALID_STATUSES
  created_at: YYYY-MM-DD date
"""

import re
import sys
from pathlib import Path

import yaml

VALID_STATUSES = {
    "runbook",
    "current",
    "planned",
    "deferred",
    "rejected",
    "completed",
    "reference",
}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check(path: Path) -> list[str]:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return [f"{path}: missing YAML frontmatter"]
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        return [f"{path}: invalid YAML frontmatter: {e}"]
    errors = []
    status = str(fm.get("status", "")).split("#")[0].strip()
    if not status:
        errors.append(
            f"{path}: missing 'status' field"
            f" (valid: {', '.join(sorted(VALID_STATUSES))})"
        )
    elif status not in VALID_STATUSES:
        errors.append(
            f"{path}: unknown status '{status}'"
            f" (valid: {', '.join(sorted(VALID_STATUSES))})"
        )
    created = fm.get("created_at")
    if not created:
        errors.append(f"{path}: missing 'created_at' field")
    elif not DATE_RE.match(str(created)):
        errors.append(
            f"{path}: 'created_at' must be YYYY-MM-DD, got '{created}'"
        )
    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <notes-dir>", file=sys.stderr)
        return 1
    notes_dir = Path(sys.argv[1])
    files = sorted(notes_dir.rglob("*.md"))
    if not files:
        print(f"no .md files found under {notes_dir}", file=sys.stderr)
        return 1
    all_errors = []
    for f in files:
        all_errors.extend(check(f))
    for err in all_errors:
        print(err)
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
