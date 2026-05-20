---
name: split_worktree_commits
description: Split a multi-finding worktree into per-finding commits via non-interactive `git add -p`. Use when an `/improve` or `/review` pass has edited several distinct findings into the worktree together and they need to land as separate commits. Refuses to run on vault-rendering templates.
---

# Split worktree commits

You have a worktree with N independent edits across one or more files. Land each finding as its own commit without losing the just-tested state.

## Pre-flight checks (always)

1. Run `git status` and `git diff --stat`. Confirm the changed files match the findings list the user is working from.
2. **Refuse to operate on files matching `templates/*.j2` that reference `*_password` / `*_token` / `*_secret`** — vault-rendering templates must be staged whole-file or not at all, per `CLAUDE.md → Commit & PR Guidelines`. If such a file is in the changeset, stage it whole-file in its own commit *before* invoking this skill on the rest.
3. Confirm working tree state is otherwise clean (no unrelated uncommitted changes you didn't introduce).

## Recipe — one finding at a time

For each finding the user wants to commit separately:

1. Identify which file(s) and which hunk(s) belong to this finding. If the user hasn't told you, ask.
2. Build the answer string for `git add -p`: one character per hunk in file order. `y` = stage, `n` = skip. If two adjacent hunks should be split apart, prepend `s` before answering y/n on the split halves.
   - Example: a file with 4 hunks, where you want to stage hunks 1 and 3 only → `printf 'y\nn\ny\nn\n' | git add -p <file>`.
3. Run the `git add -p` invocation.
4. **Always** run `git diff --staged` and visually verify the staged content matches your intent. The `printf` pattern bypasses interactive confirmation that would otherwise catch a miscount.
5. If the staged diff doesn't match → `git reset HEAD <file>` and retry. **Do not** revert the worktree to redo the edits from scratch — wastes effort, loses tested state, and risks dropping comment/decision detail.
6. Show the user the staged diff and the proposed commit message. Wait for approval.
7. `git commit` with a one-line subject + short body explaining the finding. Conventions from `CLAUDE.md → Commit & PR Guidelines`: short imperative subject, prefix with role/area when useful.
8. After commit: `git status` confirms the remaining hunks are still unstaged in the worktree.

## When hunks merge into one (and you want them split)

Git auto-merges hunks within ~3 unchanged lines of each other into a single hunk. If you need finer granularity:

- Answer `s` first when prompted on a merged hunk to split it, then answer `y`/`n` on the resulting smaller hunks.
- This shifts the answer-character count, so plan the `printf` string accordingly.

## Bail-out conditions

Stop and escalate to the user (do not silently continue) if:

- The first `git status` shows files you don't recognize from the findings list.
- Any file in the changeset matches the vault-rendering-template pattern above.
- `git diff --staged` after a `git add -p` doesn't match the intended hunk set after one retry.
- A pre-commit hook fails. Do not `--no-verify`; fix the underlying issue and create a new commit.
