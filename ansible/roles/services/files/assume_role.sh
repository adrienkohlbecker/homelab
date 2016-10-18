#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eu
set -o pipefail
IFS=$'\n\t'

RESULT=$(aws sts assume-role \
  --role-arn "$1" \
  --role-session-name "$2" \
  --duration-seconds 900)

AWS_ACCESS_KEY_ID=$(echo "$RESULT" | jq -r ".Credentials.AccessKeyId")
AWS_SECRET_ACCESS_KEY=$(echo "$RESULT" | jq -r ".Credentials.SecretAccessKey")
AWS_SESSION_TOKEN=$(echo "$RESULT" | jq -r ".Credentials.SessionToken")
AWS_REGION=eu-west-1

export AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
export AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN
export AWS_REGION=$AWS_REGION

exec ${*:3}
