#!/usr/bin/env bash
# Build a per-role test matrix from the current change set.
#
# Sources (in priority order):
#   workflow_dispatch with INPUTS_ROLES set -- explicit list (or "ALL").
#   --all on the CLI                       -- full universe (used by test-nightly.yml).
#   default (push)                         -- git diff <base>..HEAD, where
#       <base> is the newest entirely-successful run that is an ancestor of
#       HEAD -- either a ci.yml *push* run or a test-nightly run that actually
#       tested (a 25h-no-commits nightly still concludes success but validates
#       nothing, so it's filtered out). It picks the last such run on this
#       branch, or (on a feature branch with none) on the default branch
#       at/below the merge base. A red run therefore carries its change set
#       forward into the next run's matrix -- every role touched since the
#       last green state is retested, not just the latest commit's. On a push
#       with no green ancestor (or a missing/invalid token), tests the FULL
#       universe rather than risk an untrustworthy incremental diff; off a
#       push (local preview, empty dispatch) uses HEAD~1. CI_BASE_REF wins.
#
# Outputs (to $GITHUB_OUTPUT, or stdout when $GITHUB_OUTPUT is unset):
#   matrix=<JSON array of {role, variant}>  -- input to fromJson() in workflow.
#   cross_cut=true|false                    -- whether the change touches
#                                              files that affect every role
#                                              (operator gets a mail and
#                                              dispatches a targeted subset
#                                              manually).
#   packer_changed=true|false               -- whether the change touches
#                                              packer/ or mise-tasks/packer/
#                                              (gates ci.yml's packer call,
#                                              ordered before test).
#   ci_image_changed=true|false             -- whether the change touches a
#                                              ci-image.yml input on a master
#                                              push (gates ci.yml's ci-image
#                                              call, ordered before test).
#
# A change to a helper role (one with no roles/<name>/tasks/main.yml) is
# expanded via ci:role-deps into the set of consumer roles. So editing
# roles/usergroup_immediate/tasks/main.yml fans out to every role that
# imports usergroup_immediate.
#
# Local testing:
#   mise run ci:detect-roles                # no token -> diff base = HEAD~1
#   CI_BASE_REF=HEAD~5 mise run ci:detect-roles   # force a base (preview)
#   INPUTS_ROLES=foo,bar:minimal GITHUB_EVENT_NAME=workflow_dispatch mise run ci:detect-roles
#   mise run ci:detect-roles --all
set -euo pipefail

# All human-readable progress goes to stderr with a consistent prefix, so it
# shows in the GitHub step log without polluting the matrix=... stdout the
# workflow parses (in CI those land in $GITHUB_OUTPUT; locally on stdout).
log() { echo "[detect-roles] $*" >&2; }

# Universe: roles with tasks/main.yml. Helpers without main.yml fall outside
# and reach the matrix only through role-deps expansion.
UNIVERSE=$(for d in roles/*/tasks/main.yml; do
  [ -e "$d" ] || continue
  basename "$(dirname "$(dirname "$d")")"
done | sort -u)

in_universe() {
  grep -qx -- "$1" <<<"$UNIVERSE"
}

# Minimal-variant escalation list. One role per line; comments + blank
# lines OK. Each entry adds a `<role>:minimal` cell (vanilla Ubuntu
# cloud image, ext4, snapd preinstalled) on top of the default
# `<role>:box` cell. Used when behaviour depends on
# upstream-shipped packages actually being present.
MINIMAL_ROLES_FILE=".github/ci-minimal-roles.txt"
MINIMAL_ROLES=$(
  if [ -f "$MINIMAL_ROLES_FILE" ]; then
    grep -vE '^[[:space:]]*(#|$)' "$MINIMAL_ROLES_FILE" || true
  fi
)

is_minimal_role() {
  grep -qx -- "$1" <<<"$MINIMAL_ROLES"
}

# Per-role default machine, from roles/<role>/meta/test.yml's `machine:`
# key. Falls back to "box" when the file is absent or doesn't declare
# the field. Built once via a single python3 invocation rather than
# per-role to avoid ~50ms python startup * N roles. The mise lint
# task `lint:test-meta` validates the machine value against
# MACHINE_CHOICES at PR time, so detect-roles doesn't re-check.
# read -d '' returns 1 at EOF (no NUL delimiter in a heredoc), so `|| true`
# keeps it from tripping errexit; PY ends up holding the whole script body.
read -r -d '' PY <<'EOF' || true
import sys
import yaml
from pathlib import Path
for meta in sorted(Path("roles").glob("*/meta/test.yml")):
    role = meta.parent.parent.name
    try:
        data = yaml.safe_load(meta.read_text()) or {}
    except yaml.YAMLError as e:
        sys.exit(f"error: parsing {meta}: {e}")
    machine = data.get("machine")
    if machine is not None:
        print(f"{role}={machine}")
EOF
ROLE_MACHINE=$(python3 -c "$PY")

default_machine_for() {
  local role=$1 m
  m=$(grep -E "^${role}=" <<<"$ROLE_MACHINE" | head -1 | cut -d= -f2-)
  echo "${m:-box}"
}

# Cross-cut regex: changes to these paths invalidate every role's matrix
# entry, so we don't try to be clever -- emit empty matrix + cross_cut=true
# and let the operator pick a targeted subset via workflow_dispatch.
# host_vars/box.yml and host_vars/minimal.yml are the test fixtures that
# every push cell consumes; lab-qemu.yml / pug-qemu.yml only matter for
# on-demand --machine lab/pug runs and aren't cross-cut for push CI.
# test/*.py is the whole harness (testrole/testall import launch, machine,
# arch, utils, ... -- a bug in any of it mis-runs every cell), so the slice
# is all of test/*.py, not just the entrypoints. ansible.cfg and
# vault-client.sh govern every ansible invocation + vault decryption, so a
# change to either can alter any cell's behaviour.
CROSS_CUT_RE='^(group_vars/all/[^/]+\.(yml|yaml)|group_vars/test\.yml|host_vars/(box|minimal)\.yml|test/[^/]+\.py|ansible\.cfg|vault-client\.sh|mise\.toml|data/network_topology\.(yml|schema\.json))$'

emit() {
  local matrix=$1 cross_cut=$2 packer_changed=${3:-false} ci_image_changed=${4:-false}
  log "result: matrix=$matrix cross_cut=$cross_cut packer_changed=$packer_changed ci_image_changed=$ci_image_changed"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "matrix=$matrix"
      echo "cross_cut=$cross_cut"
      echo "packer_changed=$packer_changed"
      echo "ci_image_changed=$ci_image_changed"
    } >>"$GITHUB_OUTPUT"
  else
    echo "matrix=$matrix"
    echo "cross_cut=$cross_cut"
    echo "packer_changed=$packer_changed"
    echo "ci_image_changed=$ci_image_changed"
  fi
}

# Build a JSON array of "role:variant" strings from a newline-separated
# role list. Each role gets a "role:<default>" entry (default from the
# role's meta/test.yml `machine:` key, falling back to "box"); roles in
# MINIMAL_ROLES additionally get a "role:minimal" entry. lab/pug
# variants stay available via the harness for on-demand debug +
# nightly, but they don't fan out from push CI.
#
# Flat strings (not {role, variant} objects) keep workflow parsing
# simple: a single ${SPEC%%:*} / ${SPEC##*:} pair in bash gets both
# fields. The original constraint (Gitea Actions 1.21.x not expanding
# ${{ matrix.<obj>.<field> }}) is gone on GitHub Actions, but the flat
# form is still ergonomically lighter than nested-object matrix entries.
build_matrix() {
  local roles=$1
  local entries=()
  while IFS= read -r role; do
    [ -z "$role" ] && continue
    entries+=("\"$role:$(default_machine_for "$role")\"")
    if is_minimal_role "$role"; then
      entries+=("\"$role:minimal\"")
    fi
  done <<<"$roles"
  if [ ${#entries[@]} -eq 0 ]; then
    echo "[]"
  else
    local IFS=,
    echo "[${entries[*]}]"
  fi
}

# Resolve the role list from --all / INPUTS_ROLES / git diff, then emit.

if [ "${1:-}" = "--all" ]; then
  log "mode: --all (full universe)"
  emit "$(build_matrix "$UNIVERSE")" "false"
  exit 0
fi

if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ] && [ -n "${INPUTS_ROLES:-}" ]; then
  log "mode: workflow_dispatch roles='$INPUTS_ROLES'"
  if [ "$INPUTS_ROLES" = "ALL" ]; then
    log "roles=ALL -> full universe"
    emit "$(build_matrix "$UNIVERSE")" "false"
    exit 0
  fi
  # Comma-separated list. Each token is `role` (variant defaulted, with
  # minimal escalation applied if the role is in ci-minimal-roles.txt) or
  # `role:variant` (exact, no escalation -- user said what they wanted).
  # `role:lab` / `role:pug` are valid as explicit tokens but are not
  # auto-emitted on push fan-out; they exist for on-demand manual runs.
  # Output entries are "role:variant" strings; see build_matrix() for why.
  entries=()
  IFS=',' read -ra tokens <<<"$INPUTS_ROLES"
  for token in "${tokens[@]}"; do
    token=$(echo "$token" | xargs) # trim whitespace
    [ -z "$token" ] && continue
    if [[ "$token" == *:* ]]; then
      role="${token%%:*}"
      variant="${token##*:}"
      if ! in_universe "$role"; then
        echo "error: role '$role' is not in the testable universe (no roles/$role/tasks/main.yml)" >&2
        exit 1
      fi
      entries+=("\"$role:$variant\"")
    else
      role="$token"
      if ! in_universe "$role"; then
        echo "error: role '$role' is not in the testable universe (no roles/$role/tasks/main.yml)" >&2
        exit 1
      fi
      entries+=("\"$role:$(default_machine_for "$role")\"")
      if is_minimal_role "$role"; then
        entries+=("\"$role:minimal\"")
      fi
    fi
  done
  if [ ${#entries[@]} -eq 0 ]; then
    emit "[]" "false"
  else
    IFS=,
    emit "[${entries[*]}]" "false"
  fi
  exit 0
fi

# Change-detection path (push, or non-push fall-through). The diff base is
# the newest entirely-successful ci.yml push run whose commit is an ancestor
# of HEAD, so a red run's change set carries forward into the next run's
# matrix (every role touched since the last green state is retested), not
# just the latest commit's roles. Resolution order:
#   1. the last green push run on THIS branch;
#   2. on a feature branch with none of its own, the last green push run on
#      the default branch that is an ancestor of HEAD -- the merge base if it
#      was green, else the most recent green commit before it (we keep going
#      back in time until a green ancestor is found).
# When a push can't establish a green base (no green ancestor anywhere, or a
# missing/invalid token), we test the FULL universe rather than an
# untrustworthy incremental diff -- HEAD~1 would only test the latest commit
# and miss regressions in roles changed earlier but never validated.
# CI_BASE_REF overrides everything; off a push (local preview, empty
# dispatch) we use HEAD~1.
head_sha=${GITHUB_SHA:-} # guard substring below: set -u trips on unset
log "mode: change detection (event=${GITHUB_EVENT_NAME:-local}, branch=${GITHUB_REF_NAME:-?}, sha=${head_sha:0:12})"

GH_API_URL="${GITHUB_API_URL:-https://api.github.com}"
# The repo's permanent default branch. Hardcoded (env-overridable) rather
# than fetched: master has been the main line since inception and a rename
# is a one-liner here -- not worth an API round-trip on every push.
CI_DEFAULT_BRANCH="${CI_DEFAULT_BRANCH:-master}"

gh_api() {
  # GET a GitHub REST endpoint; echo the body, non-zero on HTTP error.
  curl -sS --fail-with-body \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

is_ancestor_of_head() {
  # True when commit $1 is an ancestor of GITHUB_SHA, via the compare API
  # (status "ahead" => head is ahead of base => base is an ancestor;
  # "diverged" => $1 branched off, e.g. a default-branch commit past the
  # fork point). Server-side, so it works on the shallow checkout.
  local resp status
  resp=$(gh_api "$GH_API_URL/repos/$GITHUB_REPOSITORY/compare/$1...$GITHUB_SHA" 2>/dev/null) || return 1
  status=$(jq -r '.status // empty' <<<"$resp" 2>/dev/null || true)
  [ "$status" = "ahead" ] || [ "$status" = "identical" ]
}

# Workflow files whose green runs count as a validated base: a ci.yml push
# run validates that push's diff; a test-nightly run validates the full
# universe. Matched on .path (stable across display-name changes).
CI_WORKFLOW=".github/workflows/ci.yml"
NIGHTLY_WORKFLOW=".github/workflows/test-nightly.yml"

nightly_actually_tested() {
  # True when test-nightly run $1 actually ran its matrix. The 25h-no-commits
  # gate emits an empty matrix and skips the `test` job, yet the run still
  # concludes success -- trusting that as a validated base would mask an
  # earlier red push (the changes since it would never be retested). A real
  # run has >=1 successful job other than `gate`; on a skip, `gate` is the
  # only job that runs.
  local resp
  resp=$(gh_api "$GH_API_URL/repos/$GITHUB_REPOSITORY/actions/runs/$1/jobs?per_page=100" 2>/dev/null) || return 1
  [ "$(jq -r '[.jobs[] | select(.name != "gate" and .conclusion == "success")] | length' <<<"$resp" 2>/dev/null || echo 0)" -gt 0 ]
}

newest_green_ancestor() {
  # Echo the newest entirely-successful run on branch $1 whose commit is an
  # ancestor of HEAD, or nothing. Candidates are ci.yml *push* runs (a
  # roles= dispatch only validates a subset) and test-nightly runs that
  # actually tested. status=success means every job passed. Pages back
  # through history (newest first) until a green ancestor is found or the
  # runs are exhausted, so a branch that forked far back still finds its
  # fork rather than giving up after one page.
  local branch=$1 page=1 resp count sha created path run_id
  log "  searching green ci/nightly runs on '$branch'..."
  while :; do
    resp=$(gh_api -G \
      --data-urlencode "branch=$branch" \
      --data-urlencode "status=success" \
      --data-urlencode "per_page=100" \
      --data-urlencode "page=$page" \
      "$GH_API_URL/repos/$GITHUB_REPOSITORY/actions/runs" 2>/dev/null) || {
      log "  runs query failed on '$branch' (page $page)"
      return 0
    }
    count=$(jq -r '.workflow_runs | length' <<<"$resp" 2>/dev/null || echo 0)
    [ "$count" -gt 0 ] || break
    while IFS=$'\t' read -r sha created path run_id; do
      [ -n "$sha" ] || continue
      if [ "$path" = "$NIGHTLY_WORKFLOW" ] && ! nightly_actually_tested "$run_id"; then
        log "    skip ${sha:0:12} ($created): nightly skipped its matrix (gate-only)"
        continue
      fi
      if is_ancestor_of_head "$sha"; then
        log "  green ancestor: ${sha:0:12} ($created, $(basename "$path" .yml))"
        echo "$sha"
        return 0
      fi
      log "    skip ${sha:0:12} ($created): not an ancestor of HEAD"
    done < <(jq -r --arg ci "$CI_WORKFLOW" --arg nightly "$NIGHTLY_WORKFLOW" \
      '.workflow_runs[]
       | select((.path == $ci and .event == "push") or .path == $nightly)
       | "\(.head_sha)\t\(.created_at)\t\(.path)\t\(.id)"' <<<"$resp" 2>/dev/null || true)
    page=$((page + 1))
  done
  log "  no green ancestor on '$branch'"
  return 0
}

resolve_green_base() {
  [ -n "${GITHUB_TOKEN:-}" ] || {
    log "  no GITHUB_TOKEN -- cannot query run history"
    return 0
  }
  [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_REF_NAME:-}" ] && [ -n "${GITHUB_SHA:-}" ] || return 0
  local sha
  sha=$(newest_green_ancestor "$GITHUB_REF_NAME")
  if [ -z "$sha" ] && [ "$GITHUB_REF_NAME" != "$CI_DEFAULT_BRANCH" ]; then
    log "  none on '$GITHUB_REF_NAME'; falling back to default branch '$CI_DEFAULT_BRANCH'"
    sha=$(newest_green_ancestor "$CI_DEFAULT_BRANCH")
  fi
  echo "$sha"
}

full_universe() {
  # Fallback when we can't establish a trustworthy incremental base on a
  # push: test everything. $1 is a short reason for the log.
  log "diff base: $1 -> testing the FULL universe"
  emit "$(build_matrix "$UNIVERSE")" "false"
  exit 0
}

if [ -n "${CI_BASE_REF:-}" ]; then
  BASE_REF="$CI_BASE_REF"
  log "diff base: $BASE_REF (CI_BASE_REF override)"
elif [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
  green=$(resolve_green_base)
  [ -n "$green" ] || full_universe "no green ancestor run found"
  # `git diff A B` compares the two trees directly, so only the green commit
  # object needs to be present locally -- not the history between it and
  # HEAD. If it's outside the shallow (depth-50) checkout, fetch just that
  # commit (github.com allows want-sha for ref-reachable shas).
  if ! git rev-parse --verify --quiet "${green}^{commit}" >/dev/null 2>&1; then
    log "  base ${green:0:12} outside shallow checkout; fetching the commit"
    git fetch --no-tags --quiet origin "$green" 2>/dev/null || true
  fi
  git rev-parse --verify --quiet "${green}^{commit}" >/dev/null 2>&1 ||
    full_universe "green run ${green:0:12} unreachable"
  BASE_REF="$green"
  log "diff base: ${green:0:12} (last green ci run)"
else
  BASE_REF="HEAD~1"
  log "diff base: HEAD~1 (non-push: local/preview)"
fi

if ! BASE=$(git rev-parse "$BASE_REF" 2>/dev/null); then
  full_universe "base ref '$BASE_REF' does not resolve"
fi

CHANGED=$(git diff --name-only "$BASE" HEAD)
log "comparing ${BASE:0:12}..$(git rev-parse --short HEAD): $(echo "$CHANGED" | grep -c . || true) file(s) changed"

if echo "$CHANGED" | grep -qE "$CROSS_CUT_RE"; then
  log "cross-cut paths changed -> empty matrix + operator mail:"
  echo "$CHANGED" | grep -E "$CROSS_CUT_RE" | sed 's/^/[detect-roles]     /' >&2
  emit "[]" "true"
  exit 0
fi

# Direct role detection: extract role names from `^roles/<X>/...` paths.
# For each, add the role itself if it's testable AND expand to its
# consumers via role-deps. Both are needed: most helpers (systemd_unit,
# nginx, service_user, ...) ship their own tasks/main.yml so they ARE
# testable -- but their standalone cell won't exercise a consumer-specific
# break, so a change to them must also retest every role that imports them.
# role-deps returns the consumer set (empty for a leaf role with no
# importers). The `|| true` keeps the empty case from tripping `set -o
# pipefail` -- grep -oE exits 1 with no matches (no-role-paths diff is fine).
DIRECT=$(echo "$CHANGED" | grep -oE '^roles/[^/]+' | sed 's|^roles/||' | sort -u || true)

ROLES=""
for role in $DIRECT; do
  if in_universe "$role"; then
    ROLES="$ROLES $role"
  fi
  # Expand to consumers regardless of whether the role is itself testable
  # -- a changed helper-with-main.yml is both a cell and a dependency.
  expanded=$(mise run ci:role-deps "$role" 2>/dev/null || true)
  if [ -n "$expanded" ]; then
    log "role '$role' changed -> consumers: $(echo "$expanded" | tr '\n' ' ')"
  fi
  for consumer in $expanded; do
    if in_universe "$consumer"; then
      ROLES="$ROLES $consumer"
    fi
  done
done

# Any change under packer/ or mise-tasks/packer/ (the same paths that
# trigger packer-build.yml) rebuilds the qcow2 tree and so should
# re-exercise roles/_packer's assertions against the rebuilt image.
# detect-roles only scans roles/<X>/ paths by default, so without this
# clause those edits would wait for the nightly run to catch a
# regression. packer_changed gates ci.yml's packer call, which is ordered
# before test (needs: packer), so the _packer cell only reads the qcow2s
# *after* packer-build republishes them -- firing on _packer-in-matrix
# alone would also rebuild on pushes that only touch roles/_packer/.
packer_changed=false
if echo "$CHANGED" | grep -qE '^(packer/|mise-tasks/packer/)'; then
  ROLES="$ROLES _packer"
  packer_changed=true
  log "packer inputs changed -> +_packer cell, packer_changed=true"
fi

# Any change to a ci-image.yml input means ci.yml will rebuild + republish
# the :latest image for this SHA; test must run after that so its cells
# resolve the new image. ci_image_changed gates ci.yml's ci-image call,
# which is ordered before test (needs: ci-image). The image is only
# republished on master pushes, so triple-gate on event=push + ref=master
# + input-diff; anything else (workflow_dispatch, feature-branch push, no
# input touched) leaves the boolean false and the ci-image call is skipped
# -- test never waits on a build that won't happen.
ci_image_changed=false
if [ "${GITHUB_EVENT_NAME:-}" = "push" ] &&
  [ "${GITHUB_REF:-}" = "refs/heads/master" ] &&
  echo "$CHANGED" | grep -qE '^(Dockerfile|mise\.toml|pyproject\.toml|uv\.lock|packer/qemu\.pkr\.hcl)$'; then
  ci_image_changed=true
  log "ci-image inputs changed (master push) -> ci_image_changed=true"
fi

ROLES_DEDUPED=$(echo "$ROLES" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
if [ -n "$ROLES_DEDUPED" ]; then
  log "roles to test: $(echo "$ROLES_DEDUPED" | tr '\n' ' ')"
else
  log "no role-relevant changes; matrix will be empty"
fi
emit "$(build_matrix "$ROLES_DEDUPED")" "false" "$packer_changed" "$ci_image_changed"
