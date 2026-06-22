#!/usr/bin/env bash
# Source from GitLab CI to configure AWS web-identity credentials in the current shell.
set -euo pipefail

usage() {
  echo "usage: source mise-tasks/ci/aws-oidc.sh <role-arn> <session-name> [--cache-dir] [--region <region>]" >&2
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

role_arn=$1
session_name=$2
shift 2

region=eu-central-1
create_cache=false
while [ "$#" -gt 0 ]; do
  case "$1" in
  --cache-dir)
    create_cache=true
    ;;
  --region)
    shift
    if [ "$#" -eq 0 ]; then
      usage
      exit 2
    fi
    region=$1
    ;;
  *)
    usage
    exit 2
    ;;
  esac
  shift
done

if [ -z "${GITLAB_OIDC_TOKEN:-}" ]; then
  echo "GITLAB_OIDC_TOKEN is not set; add an id_tokens entry to the GitLab job" >&2
  exit 1
fi

export AWS_WEB_IDENTITY_TOKEN_FILE
AWS_WEB_IDENTITY_TOKEN_FILE="$(pwd -P)/.aws_web_identity_token"
printf '%s' "$GITLAB_OIDC_TOKEN" >"$AWS_WEB_IDENTITY_TOKEN_FILE"
export AWS_ROLE_ARN="$role_arn"
export AWS_ROLE_SESSION_NAME="$session_name"

if [ "$create_cache" = true ]; then
  mkdir -p ~/.aws/cli/cache
fi

mise exec -- aws --region "$region" sts get-caller-identity
