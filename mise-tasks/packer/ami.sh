#!/usr/bin/env bash
#MISE description="Bake an EC2 test-cell AMI (amazon-ebssurrogate; no KVM needed) and optionally promote it"
#MISE interactive=true
#USAGE arg "<machine>" help="Machine to bake: box, pug, lab, or box_deps (derived from the promoted box AMI)"
#USAGE complete "machine" run="printf 'box\npug\nlab\nbox_deps\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
#USAGE flag "--promote" help="After a successful bake + mapping check, write the AMI id to the /homelab-ci/ami/<machine>/<ubuntu> SSM parameter the harness resolves. Without it the bake prints the candidate AMI id and the promote command."
#USAGE flag "--ssh-key <path>" help="box_deps only: private key authorized on the box AMI (CI passes the CI_CELL_SSH_KEY file variable; locally the ssh agent supplies the operator identity)"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_bake_backstop.sh
source "$(dirname "$0")/_bake_backstop.sh"

region=eu-central-1
machine="${usage_machine}"
ubuntu="${usage_ubuntu}"
# Normalised private-key copy for the box_deps seed (set below); the EXIT
# trap removes it whether or not the box_deps branch creates one.
cell_key=""

case "$machine" in
box | pug | lab | box_deps) ;;
*)
  echo "Error: machine must be box, pug, lab, or box_deps (got '$machine')" >&2
  exit 1
  ;;
esac

# box/pug/lab install from scratch under the ebssurrogate builder; box_deps
# boots the promoted box AMI and converges packer/seed_deps.yml onto it
# under amazon-ebs (see the source comment in packer/aws/ami.pkr.hcl).
only="amazon-ebssurrogate.${machine}"
extra_vars=()
if [ "$machine" = "box_deps" ]; then
  only="amazon-ebs.box_deps"
  # Resolve the parent here rather than in HCL so the derivation always
  # starts from the *promoted* box, never a most-recent candidate.
  src_ami=$(aws --region "$region" ssm get-parameter \
    --name "/homelab-ci/ami/box/${ubuntu}" \
    --query Parameter.Value --output text)
  echo "==> Deriving from promoted box AMI ${src_ami}"
  extra_vars+=(-var "box_deps_source_ami=${src_ami}")
  if [ -n "${usage_ssh_key:-}" ]; then
    # GitLab file-type CI/CD variables arrive world-readable and, worse,
    # without the trailing newline OpenSSH requires (and sometimes with CRLF):
    # the real ssh binary ansible/mitogen shells out to rejects such a key
    # ("error in libcrypto"), even though packer's Go SSH client tolerates it
    # and connects. Normalise into a private 600 copy -- strip CR, guarantee a
    # single trailing newline -- and hand both packer and ansible that copy,
    # never the original (which may be the operator's real key locally).
    cell_key=$(mktemp)
    chmod 600 "${cell_key}"
    printf '%s\n' "$(tr -d '\r' <"${usage_ssh_key}")" >"${cell_key}"
    extra_vars+=(-var "cell_ssh_key=${cell_key}")
  fi
  # The seed converge runs bare ansible-playbook (packer's ansible
  # provisioner), outside the harness that normally repairs the
  # .ansible-mitogen-strategy symlink ansible.cfg points at.
  uv run python test/setup_mitogen.py
fi

# --on-error=ask keeps the failed build instance up for SSH debugging when a
# human is at the terminal; CI gets cleanup so a failure can't leave a
# billing instance behind (same policy as packer:build).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

manifest=$(mktemp)
backstop_state=$(mktemp)
backstop_pid=""
trap 'bake_backstop_disarm "$region" "$backstop_state" "$backstop_pid"; rm -rf "$manifest" "$backstop_state" ${cell_key:+"$cell_key"}' EXIT
rm -f "$manifest" # manifest post-processor refuses to overwrite a non-manifest file

# Arm the orphan backstop (CI-only, inside the helper) in the background: it
# polls for the instance packer is about to launch and schedules its
# termination, surviving a job-timeout SIGKILL that would skip the trap above.
# box_deps tags its instance machine=box_deps, matching $machine here.
bake_backstop_arm "$region" "${CI_PIPELINE_ID:-local}" "$machine" "$ubuntu" "$backstop_state" &
backstop_pid=$!

# Run under `uv run` so the project venv is on PATH: the box_deps build's
# ansible provisioner shells out to ansible-playbook, which lives in the venv
# bin. Locally mise's uv_venv_auto sources .venv anyway, but the CI image bakes
# the venv at /opt/venv with MISE_PYTHON_UV_VENV_AUTO=false (so it does not
# shadow the baked layer), leaving it off PATH for a bare `packer build`.
uv run packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="${only}" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
  ${extra_vars[@]+"${extra_vars[@]}"} \
  packer/aws

ami=$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest["builds"][-1]["artifact_id"].split(":")[1])
' "$manifest")
echo "==> Baked ${ami}"

# Functional check on the registered mapping: a non-root volume that misses
# DeleteOnTermination=true would orphan an EBS volume after every cell
# termination — the one failure mode that bills forever (design note: every
# EBS mapping, including non-root disks, sets DeleteOnTermination=true).
# shellcheck disable=SC2016  # the backticked `false` is JMESPath syntax, not shell
leaky=$(aws --region "$region" ec2 describe-images --image-ids "$ami" \
  --query 'Images[0].BlockDeviceMappings[?Ebs != null && Ebs.DeleteOnTermination == `false`].DeviceName' \
  --output text)
if [ -n "$leaky" ]; then
  echo "Error: AMI ${ami} has mappings without DeleteOnTermination=true: ${leaky}" >&2
  echo "Deregister it and fix packer/aws/ami.pkr.hcl before promoting." >&2
  exit 1
fi
echo "==> Every EBS mapping sets DeleteOnTermination=true"

# Reap superseded AMIs for this exact (machine, ubuntu): deregister every
# self-owned homelab-ci-<machine>-<ubuntu>-* image except the two we must keep
# -- the one just promoted and the one it replaced -- deleting each one's
# backing snapshots. Without this the pipeline registers a fresh AMI plus
# several snapshots (the ZBM images map multiple volumes) on every run and
# nothing reaps the old ones, so they accumulate unbounded. Sparing the
# previous AMI preserves the one-step rollback the promote message advertises;
# anything older than that has no rollback path and just bills. The name filter
# pins both machine and release, so box never touches box_deps (the `_deps`
# breaks the `homelab-ci-box-<ubuntu>-` anchor) and jammy never touches noble.
# Best-effort throughout: a cleanup hiccup must not fail an already-good promote.
prune_superseded_amis() {
  local region="$1" machine="$2" ubuntu="$3" keep="$4" keep_prev="${5:-}" ami snaps snap
  for ami in $(aws --region "$region" ec2 describe-images --owners self \
    --filters "Name=name,Values=homelab-ci-${machine}-${ubuntu}-*" \
    --query 'Images[].ImageId' --output text 2>/dev/null); do
    [ "$ami" = "None" ] && continue
    [ "$ami" = "$keep" ] && continue
    [ -n "$keep_prev" ] && [ "$ami" = "$keep_prev" ] && continue
    snaps=$(aws --region "$region" ec2 describe-images --image-ids "$ami" \
      --query 'Images[0].BlockDeviceMappings[?Ebs != null].Ebs.SnapshotId' \
      --output text 2>/dev/null) || snaps=""
    aws --region "$region" ec2 deregister-image --image-id "$ami" >/dev/null 2>&1 || {
      echo "prune: could not deregister superseded $ami" >&2
      continue
    }
    for snap in $snaps; do
      [ "$snap" = "None" ] && continue
      aws --region "$region" ec2 delete-snapshot --snapshot-id "$snap" >/dev/null 2>&1 ||
        echo "prune: could not delete snapshot $snap" >&2
    done
    echo "==> pruned superseded ${ami} (snapshots: ${snaps:-none})"
  done
  return 0 # best-effort: never let a cleanup hiccup fail an already-good promote
}

param="/homelab-ci/ami/${machine}/${ubuntu}"
if [ "${usage_promote:-false}" != "true" ]; then
  echo "==> Candidate only (no --promote). Smoke it, then promote with:"
  echo "    aws --region ${region} ssm put-parameter --name ${param} --type String --data-type aws:ec2:image --value ${ami} --overwrite"
  exit 0
fi

# Capture the outgoing AMI before overwriting so the prune below can spare it
# (empty on the first-ever promote, when the parameter does not yet exist).
prev_ami=$(aws --region "$region" ssm get-parameter --name "$param" \
  --query Parameter.Value --output text 2>/dev/null || true)

# data-type aws:ec2:image has EC2 validate the value is a real, available
# AMI at write time, so a bad promotion fails here, not at cell launch.
aws --region "$region" ssm put-parameter \
  --name "$param" \
  --type String \
  --data-type aws:ec2:image \
  --value "$ami" \
  --overwrite
echo "==> Promoted ${ami} to ${param}"
echo "    Rollback: re-put the previous value (aws ssm get-parameter-history --name ${param})"

# Reap everything older than the promoted+previous pair (see the function note).
prune_superseded_amis "$region" "$machine" "$ubuntu" "$ami" "$prev_ami"
