# Agent skills and hooks

Repo-local skills and hook scripts live once under `.agents/` (`skills/`, `hooks/`), shared by Claude Code and Codex.

- **Skills:** Codex discovers `.agents/skills/` natively; Claude Code reads it through the `.claude/skills` symlink. Homelab-specific skills reference `AGENTS.md` (a symlink to `CLAUDE.md`), not `CLAUDE.md`, so wording stays agent-neutral.
- **Hooks** are wired per agent: `.claude/settings.json` (via `$CLAUDE_PROJECT_DIR`) and `.codex/hooks.json` (via `git rev-parse --show-toplevel`). Codex trust-hashes each entry, so re-trust with `/hooks` after editing.
- **The push gate is not an agent hook:** `mise.toml` `[hooks]` installs a git `pre-push` hook running the full `mise run lint`, so every push is gated whoever runs it.
