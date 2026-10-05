# Agent skills and hooks

Repo-local skills (`skills/`, shared by Claude Code and Codex) and Claude Code hook scripts (`hooks/`) live under `.agents/`.

- **Skills:** Codex discovers `.agents/skills/` natively; Claude Code reads it through the `.claude/skills` symlink. Homelab-specific skills reference `AGENTS.md` (a symlink to `CLAUDE.md`), not `CLAUDE.md`, so wording stays agent-neutral.
- **Hooks** are wired in `.claude/settings.json` (via `$CLAUDE_PROJECT_DIR`): worktree create/remove and post-edit validation. Codex has none — its `apply_patch` hook input carries no file path for the edit hook to validate, and its worktrees are populated by `.codex/environments/environment.toml`.
- **The push gate is not an agent hook:** `mise.toml` `[hooks]` installs a git `pre-push` hook running the full `mise run lint`, so every push is gated whoever runs it.
