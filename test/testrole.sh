#!/usr/bin/env bash
set -euxo pipefail

PASS_ARGS=("$@")
: "${ROLE:?ROLE is required}"

# On failure, pull a readable journal.
err() {
  if [ -z "$SSH_CMD" ]; then
    echo "No SSH session available for logs"
    return 0
  fi

  TMPFILE=test/out/$ROLE.journal.ansi
  if [[ "$MACHINE" == "container" ]]; then
    $PODMAN exec --tty "$(cat "$WORKDIR/$IDFILE")" env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >"$TMPFILE"
  else
    $SSH_CMD env SYSTEMD_COLORS=true journalctl --pager-end --no-pager --priority info >"$TMPFILE"
  fi
  echo "$TMPFILE"
}
trap err ERR

if [ -f "$WORKDIR/_test.yml" ]; then
  $ANSIBLE_PLAYBOOK "$WORKDIR/_test.yml"
fi

if (( RUN_CHECKMODE )); then
  LIST_TAGS=$(ansible-playbook "$WORKDIR/site.yml" --list-tags)

  $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "${PASS_ARGS[@]}"

  run_check_stage() {
    local stage=$1
    shift

    if [[ $LIST_TAGS == *"${stage}"* ]]; then
      $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --tags "$stage" "$@"
      $ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" --check "$@"
    fi
  }

  for stage in _check_stage1 _check_stage2 _check_stage3 _check_stage4; do
    run_check_stage "$stage" "${PASS_ARGS[@]}"
  done

fi

$ANSIBLE_PLAYBOOK "$WORKDIR/site.yml" "${PASS_ARGS[@]}"
