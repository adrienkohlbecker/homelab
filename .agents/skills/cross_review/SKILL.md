---
name: cross_review
description: Ask the other coding agent (Claude ↔ Codex) to review the relevant changes
argument-hint: "[pathspec|commit|range]"
disable-model-invocation: true
---

Run the review task of the **other** agent and return its review verbatim:

- If you are Claude, run `mise run codex:review $ARGUMENTS`.
- If you are Codex, run `mise run claude:review $ARGUMENTS`.

The review takes several minutes — use a generous timeout and let it finish.

Target selection is handled by the task:

- With arguments: review that pathspec, commit, or revision range.
- In a worktree with no arguments: review `master...HEAD`.
- On `master` with no arguments: review uncommitted changes, including untracked files.
- On clean `master` with no arguments: review `HEAD`.

Relay the findings as written — do not soften, dedupe, or re-judge them. You may append your own assessment after, clearly separated.
