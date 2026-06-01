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
#   matrix=<JSON array of "role:variant" strings>  -- combined matrix (all cells);
#                                              used by test-nightly.yml. A role
#                                              that gates behaviour on the Ubuntu
#                                              release also emits
#                                              "role:box:<codename>" entries
#                                              (three segments) for each release
#                                              in its meta/test.yml `ubuntu:`
#                                              list; the two-segment form defaults
#                                              the release to jammy. On the
#                                              change-detection path those release
#                                              cells also propagate onto the
#                                              changed role's consumers, so a
#                                              change to a release-gated helper
#                                              validates its dependents on that
#                                              release too.
#   matrix_jammy=<JSON array>                  -- cells where machine is box or
#                                              box_deps and release is jammy
#                                              (default). Wired to test_jammy ->
#                                              packer_jammy.
#   matrix_noble=<JSON array>                  -- cells where machine is box and
#                                              release is noble. Wired to
#                                              test_noble -> packer_noble.
#   matrix_resolute=<JSON array>               -- cells where machine is box and
#                                              release is resolute. Wired to
#                                              test_resolute -> packer_resolute.
#   matrix_minimal=<JSON array>                -- cells where machine is not
#                                              box/box_deps (minimal, lab, pug).
#                                              No packer dep. Wired to
#                                              test_minimal.
#   packer_changed=true|false               -- whether the change touches
#                                              packer/ or mise-tasks/packer/
#                                              (gates ci.yml's packer call,
#                                              ordered before test).
#   ci_image_changed=true|false             -- whether the change touches a
#                                              ci-image.yml input on a master
#                                              push (gates ci.yml's ci-image
#                                              call, ordered before test).
#   packer_sources=<JSON array>             -- the packer source matrix
#                                              ci.yml feeds to packer-build's
#                                              `build` job (folds in what
#                                              packer-build's old `prepare`
#                                              job computed). From INPUTS_SOURCES
#                                              (ci.yml's `sources` dispatch
#                                              input, space-separated); empty
#                                              -> the full set.
#   packer_sources_box=<JSON array>         -- packer_sources limited to box:
#                                              the ci.yml packer_box call matrix.
#   packer_sources_extra=<JSON array>       -- packer_sources minus box: the
#                                              packer_extra call's matrix. The
#                                              split lets test depend on box
#                                              alone (box + box_deps are the
#                                              only images push-CI tests use).
#   packer_ubuntu_box=<JSON array>          -- Ubuntu release codenames the
#                                              packer_box call crosses its source
#                                              matrix with: all supported releases
#                                              by default (box validates the build
#                                              per-release), or a single pinned
#                                              release from INPUTS_UBUNTU.
#   packer_ubuntu_extra=<JSON array>        -- same for packer_extra: jammy only
#                                              by default (pug/lab/hetzner track
#                                              prod), or the pinned release.
#
# A changed role is also expanded via ci:role-deps into the set of roles
# that import it (its consumers) -- so editing a helper like systemd_unit or
# nginx fans out to every role that uses it, not just the role's own cell.
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

# Per-role test metadata, from roles/<role>/meta/test.yml. The matrix build
# itself is handled by test/matrix.py; these lookups are used only by the
# release-cell propagation in the push path below. Built once via a single
# python3 invocation rather than per-role to avoid ~50ms startup * N roles.
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
        print(f"machine\t{role}\t{machine}")
    releases = data.get("ubuntu") or []
    if releases:
        print(f"ubuntu\t{role}\t{' '.join(releases)}")
EOF
ROLE_META=$(python3 -c "$PY")

# Default machine for a role (from meta/test.yml, falls back to "box").
# Used by release-cell propagation to emit consumer:machine:codename specs.
default_machine_for() {
  local role=$1 m
  m=$(awk -F'\t' -v r="$role" '$1 == "machine" && $2 == r {print $3; exit}' <<<"$ROLE_META")
  echo "${m:-box}"
}

# Extra Ubuntu release codenames a role declares (space-separated; empty when
# none). Used by the release-cell propagation in the push path.
release_ubuntu_for() {
  awk -F'\t' -v r="$1" '$1 == "ubuntu" && $2 == r {print $3}' <<<"$ROLE_META"
}

# ---------------------------------------------------------------------------
# Path patterns -- edit here. Each group is a list of unanchored EREs (one
# per line, trailing comment = why); join_re OR-joins them and each consumer
# adds its own anchors. Keep the per-pattern comments current.
# ---------------------------------------------------------------------------
join_re() {
  local IFS='|'
  printf '%s' "$*"
}

# Full-universe triggers: a change to any of these can't be attributed to
# specific roles (it affects every cell), so we test the whole universe
# rather than a scoped subset.
FULL_UNIVERSE_PATTERNS=(
  'group_vars/all/[^/]+\.(yml|yaml)'          # shared defaults every role reads
  'group_vars/test\.yml'                      # test-scope vars/vault
  'host_vars/(box|minimal)\.yml'              # push-CI fixtures (lab/pug host_vars don't trigger a wide run)
  'test/[^/]+\.py'                            # harness modules (testrole/testall import launch, machine, arch, utils)
  'test/inventory\.ini'                       # shared inventory
  'test/(playbooks|minimal)/.+'               # wrapper playbooks (site/_setup/_verify/_mirrors) + minimal cloud-init seed
  'ansible\.cfg'                              # governs every ansible invocation
  'vault-client\.sh'                          # governs vault decryption
  'mise\.toml'                                # toolchain + env (also a ci-image input)
  'pyproject\.toml'                           # harness python deps (also a ci-image input)
  'uv\.lock'                                  # pinned harness python deps (also a ci-image input)
  'data/network_topology\.(yml|schema\.json)' # topology consumed across roles
)
FULL_UNIVERSE_RE="^($(join_re "${FULL_UNIVERSE_PATTERNS[@]}"))\$"

# Packer inputs: rebuild the qcow2 tree (packer_changed) + add the packer
# cell. Prefix match -- any file under these dirs.
PACKER_PATH_PATTERNS=(
  'packer/'            # image build definitions + provisioning scripts
  'mise-tasks/packer/' # the packer:* task wrappers
)
PACKER_PATHS_RE="^($(join_re "${PACKER_PATH_PATTERNS[@]}"))"

# ci-image inputs: a change here (on a master push) rebuilds + republishes
# the ci :latest image (ci_image_changed). Exact full-path match.
CI_IMAGE_INPUT_PATTERNS=(
  'Dockerfile'            # the image recipe
  'mise\.toml'            # toolchain pinned into the image
  'pyproject\.toml'       # python deps baked into the image
  'uv\.lock'              # pinned python deps
  'packer/qemu\.pkr\.hcl' # packer plugins pre-installed in the image
)
CI_IMAGE_INPUTS_RE="^($(join_re "${CI_IMAGE_INPUT_PATTERNS[@]}"))\$"

# Role path: pull the role name out of roles/<name>/... (used with grep -oE,
# so it matches just the leading segment).
ROLE_PATH_RE='^roles/[^/]+'

# Matrix bucket splitting, packer source/ubuntu computation, and CI output
# emission are handled by mise-tasks/ci/detect.py (unit-tested in Python).
# This thin wrapper preserves the call-site interface ($1=matrix, $2=packer,
# $3=ci_image) so the rest of the script is unchanged.
emit() {
  local matrix=$1 packer_changed=${2:-false} ci_image_changed=${3:-false}
  python3 mise-tasks/ci/detect.py emit \
    --matrix "$matrix" \
    --packer-changed "$packer_changed" \
    --ci-image-changed "$ci_image_changed" \
    --inputs-sources "${INPUTS_SOURCES:-}" \
    --inputs-ubuntu "${INPUTS_UBUNTU:-}"
}

# Resolve the role list from --all / INPUTS_ROLES / git diff, then emit.
# Matrix building is delegated to test/matrix.py (the single source of
# truth shared with test/testall.py); this script handles change detection,
# role-deps expansion, and release-cell propagation, then hands the role
# list + extra cells to matrix.py for the final JSON.

if [ "${1:-}" = "--all" ]; then
  log "mode: --all (full universe)"
  emit "$(python3 test/matrix.py --json --all)"
  exit 0
fi

if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ] && [ -n "${INPUTS_ROLES:-}" ]; then
  log "mode: workflow_dispatch roles='$INPUTS_ROLES'"
  if [ "$INPUTS_ROLES" = "ALL" ]; then
    log "roles=ALL -> full universe"
    emit "$(python3 test/matrix.py --json --all)"
    exit 0
  fi
  # Comma-separated list. Each token is `role` (expanded with default
  # machine + minimal + release escalation) or `role:variant` (exact, no
  # escalation). Parsing + universe validation delegated to matrix.py.
  emit "$(python3 test/matrix.py --json --dispatch "$INPUTS_ROLES")"
  exit 0
fi

# A dispatch that sets `sources` but no `roles` is an explicit packer-only
# rebuild (the entry point that replaced packer-build.yml's standalone
# workflow_dispatch). Force packer_changed=true so ci.yml's packer call fires
# regardless of git diff, and emit an empty role matrix so test is skipped --
# rebuilding just lab/pug doesn't drag the test fan-out along. A dispatch with
# `roles` ALSO set returned via the INPUTS_ROLES branch above, so we only reach
# here when roles is empty.
if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ] && [ -n "${INPUTS_SOURCES:-}" ]; then
  log "mode: workflow_dispatch sources='$INPUTS_SOURCES' (packer-only, empty test matrix)"
  emit "[]" true false
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
  # Retry transient failures: the Actions API intermittently 404s / 5xxs
  # under load, and a single blip here otherwise collapses green-base
  # resolution into a needless full-universe run. --retry-all-errors so the
  # retry also covers the 404s that plain --retry (5xx/429/timeouts only)
  # skips; these endpoints never legitimately 404 (the repo exists).
  curl -sS --fail-with-body \
    --retry 4 --retry-delay 2 --retry-all-errors \
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
  # Test everything. $1 is a short reason for the log; $2/$3 carry the
  # substrate-rebuild flags through (default false) -- a full-universe run
  # triggered by a uv.lock/packer change still needs the image/qcow2 rebuilt.
  log "$1 -> testing the FULL universe"
  emit "$(python3 test/matrix.py --json --all)" "${2:-false}" "${3:-false}"
  exit 0
}

if [ -n "${CI_BASE_REF:-}" ]; then
  BASE_REF="$CI_BASE_REF"
  log "diff base: $BASE_REF (CI_BASE_REF override)"
elif [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
  green=$(resolve_green_base)
  # No base (genuinely none, or the API stayed down through gh_api's
  # retries): test everything AND rebuild the substrate. A full-universe run
  # exercises box_deps cells, which can't boot without a freshly-seeded
  # box_deps image, so packer must run -- otherwise every box_deps cell dies
  # on a missing artifact. (ci-image is left alone: the published :latest is
  # serviceable, and a speculative rebuild on every base-miss isn't worth the
  # minutes.)
  [ -n "$green" ] || full_universe "no green ancestor run found" true
  # `git diff A B` compares the two trees directly, so only the green commit
  # object needs to be present locally -- not the history between it and
  # HEAD. If it's outside the shallow (depth-50) checkout, fetch just that
  # commit (github.com allows want-sha for ref-reachable shas).
  if ! git rev-parse --verify --quiet "${green}^{commit}" >/dev/null 2>&1; then
    log "  base ${green:0:12} outside shallow checkout; fetching the commit"
    git fetch --no-tags --quiet origin "$green" 2>/dev/null || true
  fi
  git rev-parse --verify --quiet "${green}^{commit}" >/dev/null 2>&1 ||
    full_universe "green run ${green:0:12} unreachable" true
  BASE_REF="$green"
  log "diff base: ${green:0:12} (last green ci run)"
else
  BASE_REF="HEAD~1"
  log "diff base: HEAD~1 (non-push: local/preview)"
fi

if ! BASE=$(git rev-parse "$BASE_REF" 2>/dev/null); then
  full_universe "base ref '$BASE_REF' does not resolve" true
fi

CHANGED=$(git diff --name-only "$BASE" HEAD)
log "comparing ${BASE:0:12}..$(git rev-parse --short HEAD): $(echo "$CHANGED" | grep -c . || true) file(s) changed"

# Substrate-rebuild flags, computed BEFORE the full-universe decision so
# they survive it: a uv.lock / mise.toml bump is BOTH a full-universe
# trigger AND a ci-image input -- the image must still rebuild before the
# wide run (else cells resolve stale deps), and a packer change riding along
# with a full-universe trigger must still rebuild the qcow2 tree.
#
# packer_changed: any packer/ or mise-tasks/packer/ change rebuilds the
# qcow2 tree via ci.yml's packer call (ordered before test, needs: packer).
# The packer cell, added below on the role path, re-exercises roles/packer
# against the rebuilt image.
packer_changed=false
if echo "$CHANGED" | grep -qE "$PACKER_PATHS_RE"; then
  packer_changed=true
  log "packer inputs changed -> packer_changed=true"
fi

# ci_image_changed gates ci.yml's ci-image call (ordered before test,
# needs: ci-image). The image is only republished on master pushes, so
# triple-gate on event=push + ref=master + input-diff; anything else leaves
# it false and the ci-image call is skipped -- test never waits on a build
# that won't happen.
ci_image_changed=false
if [ "${GITHUB_EVENT_NAME:-}" = "push" ] &&
  [ "${GITHUB_REF:-}" = "refs/heads/master" ] &&
  echo "$CHANGED" | grep -qE "$CI_IMAGE_INPUTS_RE"; then
  ci_image_changed=true
  log "ci-image inputs changed (master push) -> ci_image_changed=true"
fi

# Full-universe trigger: the change can't be attributed to specific roles,
# so test everything -- carrying the substrate flags so the image / qcow2
# tree rebuild alongside (the full run reads them).
if echo "$CHANGED" | grep -qE "$FULL_UNIVERSE_RE"; then
  log "full-universe paths changed:"
  echo "$CHANGED" | grep -E "$FULL_UNIVERSE_RE" | sed 's/^/[detect-roles]     /' >&2
  full_universe "full-universe path changed" "$packer_changed" "$ci_image_changed"
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
DIRECT=$(echo "$CHANGED" | grep -oE "$ROLE_PATH_RE" | sed 's|^roles/||' | sort -u || true)

ROLES=""
# Propagated release cells ("<consumer>:<machine>:<codename>"),
# newline-separated -- a changed release-gated role's release set fanned
# out onto its consumers (matrix.py de-dups + sorts). The consumer's own
# default machine is used so a box_deps consumer gets a box_deps release
# cell. Stays empty when no changed role declares `ubuntu:`.
RELEASE_CELLS=""
# A packer change rebuilds the qcow2 tree, so test roles/packer against it.
if [ "$packer_changed" = true ]; then
  ROLES="packer"
fi
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
  # Propagate the changed role's declared release cells onto every consumer
  # that imports it: a change to a release-gated helper (e.g. apt_source's
  # >= 26 deb822 branch) validates its dependents on that release, not just
  # the helper's own standalone release cell. The changed role itself is
  # already covered by matrix.py's per-role release expansion; this fans
  # the same releases out to its consumers. `expanded` already includes the
  # role itself, so the self-cell it adds is a harmless dedup'd duplicate.
  releases=$(release_ubuntu_for "$role")
  if [ -n "$releases" ] && [ -n "$expanded" ]; then
    log "  propagating release cells [$releases] from '$role' to its consumers"
    for consumer in $expanded; do
      in_universe "$consumer" || continue
      for codename in $releases; do
        RELEASE_CELLS="$RELEASE_CELLS"$'\n'"$consumer:$(default_machine_for "$consumer"):$codename"
      done
    done
  fi
done

ROLES_DEDUPED=$(echo "$ROLES" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
if [ -n "$ROLES_DEDUPED" ]; then
  log "roles to test: $(echo "$ROLES_DEDUPED" | tr '\n' ' ')"
else
  log "no role-relevant changes; matrix will be empty"
fi
# shellcheck disable=SC2086
emit "$(python3 test/matrix.py --json ${RELEASE_CELLS:+--extra $RELEASE_CELLS --} $ROLES_DEDUPED)" "$packer_changed" "$ci_image_changed"
