#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Threshold of deleted files over which to show an error
# Useful in case of mass deletion by human error / missing device
DEL_THRESHOLD=50

# Destination email
EMAIL_TO="root"

# Email subject prefix
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] snapraid sync"

# Collect output for parsing / emails
TMP_OUTPUT=$(mktemp)
trap 'rm -rf $TMP_OUTPUT' EXIT

set +e
snapraid diff | tee $TMP_OUTPUT
DIFF_RETVAL=$?
set -e

if [ $DIFF_RETVAL -ne 0 -a $DIFF_RETVAL -ne 2 ]; then
  exit 1
fi

DEL_COUNT=$(grep '[0-9]\{1,\} removed$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
ADD_COUNT=$(grep '[0-9]\{1,\} added$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
MOVE_COUNT=$(grep '[0-9]\{1,\} moved$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
COPY_COUNT=$(grep '[0-9]\{1,\} copied$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
UPDATE_COUNT=$(grep '[0-9]\{1,\} updated$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
RESTORE_COUNT=$(grep '[0-9]\{1,\} restored$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)

echo "SUMMARY of changes - Added [$ADD_COUNT] - Deleted [$DEL_COUNT] - Moved [$MOVE_COUNT] - Copied [$COPY_COUNT] - Updated [$UPDATE_COUNT] - Restored [$RESTORE_COUNT]"

# Check if files have changed
if [ $DIFF_RETVAL -eq 2 ]; then
  # YES, check if number of deleted files exceed DEL_THRESHOLD
  if [ "$DEL_COUNT" -gt $DEL_THRESHOLD ]; then
    # YES, lets inform user and not proceed with the sync just in case
    echo "Number of deleted files ($DEL_COUNT) exceeded threshold ($DEL_THRESHOLD)."
    echo "NOT proceeding with sync job. Please run sync manually if this is not an error condition."
    mail -s "$EMAIL_SUBJECT_PREFIX WARNING - Number of deleted files ($DEL_COUNT) exceeded threshold ($DEL_THRESHOLD)" "$EMAIL_TO" < "$TMP_OUTPUT"
    exit 1
  else
    # NO, delete threshold not reached, lets run the sync job
    echo "Deleted files ($DEL_COUNT) did not exceed threshold ($DEL_THRESHOLD), proceeding with sync job."
    snapraid sync
  fi
else
  # NO, so lets log it and exit
  echo "No change detected. Nothing to do"
fi

echo "Done"
