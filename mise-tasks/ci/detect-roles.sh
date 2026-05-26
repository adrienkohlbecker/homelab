#!/usr/bin/env bash
# Build a per-role test matrix from the current change set.
#
# Sources (in priority order):
#   workflow_dispatch with INPUTS_ROLES set -- explicit list (or "ALL").
#   --all on the CLI                       -- full universe (used by test-nightly.yml).
#   default                                -- git diff HEAD~1 HEAD.
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
#                                              (gates wait-for-packer-build).
#   ci_image_changed=true|false             -- whether the change touches a
#                                              ci-image.yml input on a master
#                                              push (gates wait-for-ci-image).
#
# A change to a helper role (one with no roles/<name>/tasks/main.yml) is
# expanded via ci:role-deps into the set of consumer roles. So editing
# roles/usergroup_immediate/tasks/main.yml fans out to every role that
# imports usergroup_immediate.
#
# Local testing:
#   mise run ci:detect-roles                # uses git diff HEAD~1 HEAD
#   INPUTS_ROLES=foo,bar:minimal GITHUB_EVENT_NAME=workflow_dispatch mise run ci:detect-roles
#   mise run ci:detect-roles --all
set -euo pipefail

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
ROLE_MACHINE=$(python3 <<'EOF'
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
)

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
CROSS_CUT_RE='^(group_vars/all/[^/]+\.(yml|yaml)|group_vars/test\.yml|host_vars/(box|minimal)\.yml|test/(testrole|testall|machine)\.py|mise\.toml|data/network_topology\.(yml|schema\.json))$'

emit() {
  local matrix=$1 cross_cut=$2 packer_changed=${3:-false} ci_image_changed=${4:-false}
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
  emit "$(build_matrix "$UNIVERSE")" "false"
  exit 0
fi

if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ] && [ -n "${INPUTS_ROLES:-}" ]; then
  if [ "$INPUTS_ROLES" = "ALL" ]; then
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

# Push event (or default): diff HEAD~1 HEAD. CI_BASE_REF overrides the base
# revision (lets unit tests exercise the diff logic against a chosen point;
# CI itself just leaves it unset).
BASE_REF="${CI_BASE_REF:-HEAD~1}"
if ! BASE=$(git rev-parse "$BASE_REF" 2>/dev/null); then
  echo "no $BASE_REF -- treating as cross-cut so the operator can dispatch a roles=ALL run" >&2
  emit "[]" "true"
  exit 0
fi

CHANGED=$(git diff --name-only "$BASE" HEAD)

if echo "$CHANGED" | grep -qE "$CROSS_CUT_RE"; then
  echo "cross-cut detected:" >&2
  echo "$CHANGED" | grep -E "$CROSS_CUT_RE" | sed 's/^/  /' >&2
  emit "[]" "true"
  exit 0
fi

# Direct role detection: extract role names from `^roles/<X>/...` paths,
# then for each one either add to the matrix (if in universe) or expand
# via role-deps (if it's a helper). The `|| true` keeps the empty case
# from tripping `set -o pipefail` -- grep -oE exits 1 with no matches,
# which is fine here (no-role-paths diff is normal).
DIRECT=$(echo "$CHANGED" | grep -oE '^roles/[^/]+' | sed 's|^roles/||' | sort -u || true)

ROLES=""
for role in $DIRECT; do
  if in_universe "$role"; then
    ROLES="$ROLES $role"
    continue
  fi
  # Helper role: expand to consumers (intersected with universe).
  expanded=$(mise run ci:role-deps "$role" 2>/dev/null || true)
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
# regression. packer_changed is consumed by test.yml's
# wait-for-packer-build job so the _packer cell only reads the qcow2s
# *after* packer-build finishes -- firing on _packer-in-matrix alone
# would also block pushes that only touch roles/_packer/.
packer_changed=false
if echo "$CHANGED" | grep -qE '^(packer/|mise-tasks/packer/)'; then
  ROLES="$ROLES _packer"
  packer_changed=true
fi

# Any change to a ci-image.yml input means ci-image.yml is publishing
# a new :latest for this SHA; downstream workflows must wait on
# wait-for-ci-image before resolving their container: blocks.
# ci-image.yml only fires on master pushes (branches: [master] in its
# `on:`), so triple-gate on event=push + ref=master + input-diff;
# anything else (workflow_dispatch, feature-branch push, no input
# touched) leaves the boolean false and the waiter is skipped --
# downstream never blocks on a build that won't happen.
ci_image_changed=false
if [ "${GITHUB_EVENT_NAME:-}" = "push" ] &&
  [ "${GITHUB_REF:-}" = "refs/heads/master" ] &&
  echo "$CHANGED" | grep -qE '^(Dockerfile|mise\.toml|pyproject\.toml|uv\.lock|packer/qemu\.pkr\.hcl)$'; then
  ci_image_changed=true
fi

ROLES_DEDUPED=$(echo "$ROLES" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
emit "$(build_matrix "$ROLES_DEDUPED")" "false" "$packer_changed" "$ci_image_changed"
