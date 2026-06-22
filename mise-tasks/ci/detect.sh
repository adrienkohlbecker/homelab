#!/usr/bin/env bash
#MISE description="Render the GitLab role-test child pipeline"
#USAGE flag "--target <target>" help="Qemu target to render: aws_qemu or lab"
#USAGE flag "--child-path <child_path>" help="Generated child pipeline path" default="test-child.yml"
#USAGE flag "--all" help="Force the full test universe"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

target="${usage_target:-${HOMELAB_CI_TARGET:-aws_qemu}}"
args=(gitlab --target "$target" --child-path "$usage_child_path")
if [ "${usage_all:-false}" = "true" ]; then
  args+=(--all)
fi

mise exec -- uv run python mise-tasks/ci/detect.py "${args[@]}"
