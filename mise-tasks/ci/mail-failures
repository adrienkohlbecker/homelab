#!/usr/bin/env bash
# Notify the operator that the nightly matrix produced failures.
#
# Called from the notify job of test-nightly.yml when test-role's overall
# result is failure (or cancelled). Mails a short pointer to the run page;
# the per-cell artifacts on the run page have the journal/dmesg/boot.log
# detail.
#
# Required env (Gitea repo secrets, set in the workflow's `env:`):
#   MAILGUN_API_KEY        -- the Mailgun API key (api:<key>)
#   MAILGUN_DOMAIN         -- the Mailgun-controlled domain (e.g. noreply.fahm.fr)
#
# Optional env:
#   NEEDS_RESULT           -- aggregate matrix outcome (success | failure | cancelled)
#                             passed as ${{ toJson(needs.test-role.result) }}
#   MAIL_TO                -- override recipient (default adrien.kohlbecker@gmail.com)
#   GITHUB_SHA, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID -- standard Actions env
set -euo pipefail

: "${MAILGUN_API_KEY:?MAILGUN_API_KEY must be set}"
: "${MAILGUN_DOMAIN:?MAILGUN_DOMAIN must be set}"

mail_to="${MAIL_TO:-adrien.kohlbecker@gmail.com}"
sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
short_sha="${sha:0:8}"
result="${NEEDS_RESULT:-unknown}"
result=${result//\"/} # NEEDS_RESULT comes through as a JSON string with quotes; strip them
run_url="${GITHUB_SERVER_URL:-https://gitea.example}/${GITHUB_REPOSITORY:-repo}/actions/runs/${GITHUB_RUN_ID:-?}"

body=$(
  cat <<EOF
Commit: $short_sha
Result: $result

Per-cell logs are attached to each failed test-role job in the run page.
Each failed cell uploads its harness output, journal, dmesg, and the
list of failed systemd units as a downloadable artifact.

Run: $run_url
EOF
)

curl -sS --fail-with-body \
  --user "api:$MAILGUN_API_KEY" \
  "https://api.eu.mailgun.net/v3/$MAILGUN_DOMAIN/messages" \
  -F from="homelab-ci@$MAILGUN_DOMAIN" \
  -F to="$mail_to" \
  -F subject="[homelab CI] nightly $result on $short_sha" \
  -F text="$body" >/dev/null

echo "mail sent to $mail_to (nightly $result on $short_sha)"
