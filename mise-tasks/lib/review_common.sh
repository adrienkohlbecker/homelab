#!/usr/bin/env bash
# Shared core for the claude:review / codex:review file tasks: repo + scope
# resolution, diff assembly, and the agent-neutral review prompt. Sourced, not
# executed — no exec bit, so mise does not register it as a task.
#
# The sourcing script must set, before sourcing:
#   agent_label  -- name used in messages and temp files ("claude-review", ...)
#   base_ref     -- base ref for worktree diffs
#   dry_run      -- "1" to print the assembled prompt instead of running
# and define run_agent(), which sends $prompt_file to its agent and prints the
# review. run_agent may read $repo and $scope. After sourcing, the script calls
# review_main "$@".

: "${agent_label:?sourcing script must set agent_label}"
: "${base_ref:?sourcing script must set base_ref}"

# mise runs file tasks from the main checkout's PWD even when invoked from a
# linked worktree nested under it, so resolve the repo from the directory the
# user actually invoked from (MISE_ORIGINAL_CWD) to review the worktree they are
# standing in. Falls back to PWD when run outside mise.
cd "${MISE_ORIGINAL_CWD:-$PWD}" || exit 1
repo=$(git rev-parse --show-toplevel)
cd "$repo" || exit 1

main_worktree=$(git worktree list --porcelain | awk '/^worktree / {print $2; exit}')
current_branch=$(git branch --show-current)

prompt_file=$(mktemp -t "$agent_label.XXXXXX.md")
diff_file=$(mktemp -t "$agent_label.XXXXXX.diff")
trap 'rm -f "$prompt_file" "$diff_file"' EXIT

has_untracked() {
  local first
  first=$(git ls-files --others --exclude-standard -- "$@" | awk 'NR == 1 {print; exit}')
  [ -n "$first" ]
}

append_untracked_diffs() {
  local file

  while IFS= read -r -d '' file; do
    [ -f "$file" ] || continue
    printf '\n' >>"$diff_file"
    git diff --no-index -- /dev/null "$file" >>"$diff_file" || true
  done < <(git ls-files -z --others --exclude-standard -- "$@")
}

append_local_diffs() {
  if ! git diff --quiet HEAD -- "$@"; then
    {
      printf '\n'
      printf '# Local tracked changes\n'
    } >>"$diff_file"
    git diff --stat --patch --find-renames --find-copies HEAD -- "$@" >>"$diff_file"
  fi

  if has_untracked "$@"; then
    {
      printf '\n'
      printf '# Local untracked files\n'
    } >>"$diff_file"
    append_untracked_diffs "$@"
  fi
}

write_review_prompt() {
  cat >"$prompt_file" <<EOF
You are an expert code reviewer working INSIDE a live checkout of the homelab
repository (an Ansible / infrastructure monorepo). This is not a paper review:
your working directory IS the repo, and you have read-only tools (file reads,
grep, read-only shell commands like git log / git blame / git show, plus web
search and fetch where available). Use them aggressively. A review that only
reads the diff is shallow and not what is wanted here.

You are running under a read-only sandbox. Do not edit files, stage changes,
commit, push, or run state-mutating commands. Diagnostic reads are expected.

Scope: $scope
Repository: $repo
Branch: ${current_branch:-detached}

Investigate before you report. For every hunk:
- Open the changed files and read the surrounding code the diff omits — the
  callers, the callees, the producer of any value consumed here, the consumer
  of any value produced here. Most real bugs live in the seam between the diff
  and the code it touches, which the diff does not show.
- VERIFY every claim the code makes rather than trusting it:
  - Comments & docstrings: does the comment still describe what the code now
    does? Stale, misleading, or aspirational comments are findings.
  - Assumptions: if the code assumes a file/dir/var/secret/unit exists, a
    command is on PATH, a value has a given type/shape, or a default applies —
    confirm it against the actual tree (grep for the definition, read the
    producer, check the helper/role that is supposed to create it).
  - API / library / CLI usage: confirm flags, subcommands, function signatures,
    return shapes and behavior against ground truth — vendored source, installed
    \`--help\`/man output, the role's vars/main.yml, or upstream docs on the
    web. Flag invented flags, wrong signatures, deprecated forms, and
    version-skew between what the code calls and what is actually available.
  - Cross-references: when the diff names a var, role, task, port, secret,
    systemd unit, dataset, or network defined elsewhere, open that definition
    and confirm name, type, default, scope and precedence line up. Ansible var
    precedence (vars/main.yml above host_vars), service_ports as the single
    source of truth, and helper \`*_args\` dicts are common failure points.
- Check repo conventions: AGENTS.md at the repo root (a symlink to CLAUDE.md —
  the same file) encodes hard rules and load-bearing idioms — no handlers for
  restarts, no \`no_log\`, \`set -euo pipefail\` in every shell block,
  \`backup: true\` on file writes, helper roles over hand-rolled boilerplate,
  system-scope systemd, etc. Flag violations with the specific rule.

Hunt for: correctness bugs, behavioral regressions, idempotence breaks,
security/privacy leaks (vault, plaintext secrets, PII, over-broad perms),
error-handling and edge-case gaps, races/ordering hazards, missing or wrong
tests (\`_verify.yml\`), and maintainability surprises.

Method: form a hypothesis from the diff, then PROVE or DISPROVE it against the
real tree before writing it down. Do not speculate when you can check. When you
assert something is wrong, cite the evidence — the exact file:line you read or
the command output that confirmed it. Distinguish "verified" from "suspected"
and say which.

Output format:
- Lead with findings, ordered by severity: Critical, High, Medium, Low, Nit.
- Each finding: severity tag, \`file:line\` (or diff hunk), what is wrong, why it
  matters, the evidence you used to confirm it, and a concrete suggested fix.
- A brief summary AFTER the findings.
- If you genuinely find nothing, say so plainly, then state what you verified
  and what residual risk or test gap remains. Do not pad.

Be thorough, concrete, and detailed. The selected changes follow.

\`\`\`diff
EOF
  cat "$diff_file" >>"$prompt_file"
  printf '\n```\n' >>"$prompt_file"
}

review_main() {
  # An empty "$@" is an empty git pathspec, i.e. the whole tree, so the
  # with-arguments and without-arguments cases share one cascade. Only the
  # range/commit forms need an actual argument.
  if [ "$#" -eq 1 ] && [[ "$1" == *..* ]]; then
    scope="explicit revision range: $1"
    git diff --stat --patch --find-renames --find-copies "$1" >"$diff_file"
  elif [ "$#" -eq 1 ] && git rev-parse --verify -q "$1^{commit}" >/dev/null; then
    scope="explicit commit: $1"
    git show --stat --patch --find-renames --find-copies --format=fuller "$1" >"$diff_file"
  elif [ "$repo" != "$main_worktree" ]; then
    scope="worktree changes: $base_ref...HEAD plus local changes${*:+ -- $*}"
    git diff --stat --patch --find-renames --find-copies "$base_ref...HEAD" -- "$@" >"$diff_file"
    append_local_diffs "$@"
  elif ! git diff --quiet HEAD -- "$@" || has_untracked "$@"; then
    scope="uncommitted changes on $current_branch${*:+ -- $*}"
    git diff --stat --patch --find-renames --find-copies HEAD -- "$@" >"$diff_file"
    append_untracked_diffs "$@"
  else
    local last_commit
    last_commit=$(git log -1 --format=%H -- "$@" || true)
    if [ -z "$last_commit" ]; then
      echo "$agent_label: no changes or commits found for pathspec: $*" >&2
      exit 1
    fi
    scope="last commit on $current_branch: $last_commit${*:+ -- $*}"
    git show --stat --patch --find-renames --find-copies --format=fuller "$last_commit" -- "$@" >"$diff_file"
  fi

  if [ ! -s "$diff_file" ]; then
    echo "$agent_label: selected scope produced an empty diff: $scope" >&2
    exit 1
  fi

  write_review_prompt

  if [ "${dry_run:-}" = "1" ]; then
    cat "$prompt_file"
    return
  fi

  run_agent
}
