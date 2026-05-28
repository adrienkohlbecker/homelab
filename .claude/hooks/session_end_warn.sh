#!/usr/bin/env bash
# Claude Code SessionEnd hook: warn the user if the notes/ submodule has
# uncommitted changes -- the two-step submodule dance (commit+push inside
# notes/, then `git add notes` in the parent) can otherwise strand them.
# SessionEnd cannot block; exit 2 surfaces stderr to the user.
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
[ -e "$root/notes/.git" ] || exit 0

dirty=$(git -C "$root/notes" status --porcelain 2>/dev/null) || exit 0
if [ -n "$dirty" ]; then
  echo "notes/ submodule has uncommitted changes -- commit & push inside notes/, then 'git add notes' in the parent (CLAUDE.md: notes submodule)." >&2
  exit 2
fi
