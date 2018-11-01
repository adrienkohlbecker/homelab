#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Destination email
EMAIL_TO="root"

# Email subject prefix
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] snapraid scrub"

# Collect output for parsing / emails
TMP_OUTPUT=$(mktemp)
trap 'rm -rf $TMP_OUTPUT' EXIT

snapraid scrub | tee $TMP_OUTPUT

if grep -q "Everything OK" "$TMP_OUTPUT"; then
  echo "Everything looks good"
elif grep -q "WARNING! There are errors" "$TMP_OUTPUT"; then
  READ_COUNT=$(grep '[0-9]\{1,\} read errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
  DATA_COUNT=$(grep '[0-9]\{1,\} data errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)

  echo "Scrub errors summary: Read [$READ_COUNT] - Data [$DATA_COUNT]"
  mail -s "$EMAIL_SUBJECT_PREFIX - Errors found during scrub" "$EMAIL_TO" < "$TMP_OUTPUT"
  exit 1
else
  echo "An unexpected error has happened."
  mail -s "$EMAIL_SUBJECT_PREFIX ERROR - An unexpected error has happended during scrub" "$EMAIL_TO" < "$TMP_OUTPUT"
  exit 1
fi

echo "Done"
