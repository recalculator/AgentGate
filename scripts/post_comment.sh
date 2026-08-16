#!/usr/bin/env bash
# Post or update the single Agent Gate comment on a pull request.
#
# One comment per PR, edited in place on every push, so the timeline shows the
# current verdict rather than a stack of stale ones. Identified by the marker
# that `agent-gate --format markdown` embeds.
#
# Required env: GH_TOKEN, REPO (owner/name), PR_NUMBER, REPORT (path to markdown)
set -uo pipefail

MARKER="<!-- agent-gate:comment -->"

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${REPO:?REPO is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"
: "${REPORT:?REPORT is required}"

if [ ! -f "$REPORT" ]; then
  echo "::warning::No report at $REPORT — skipping PR comment."
  exit 0
fi

# Find an existing Agent Gate comment, if any.
EXISTING=$(gh api --paginate "repos/${REPO}/issues/${PR_NUMBER}/comments" \
  --jq "map(select(.body | contains(\"${MARKER}\"))) | .[0].id // empty" 2>/dev/null || true)

if [ -n "$EXISTING" ]; then
  if gh api -X PATCH "repos/${REPO}/issues/comments/${EXISTING}" \
      -F body=@"$REPORT" >/dev/null 2>&1; then
    echo "updated comment ${EXISTING}"
    exit 0
  fi
  echo "::warning::Could not update comment ${EXISTING}; posting a new one."
fi

if gh api -X POST "repos/${REPO}/issues/${PR_NUMBER}/comments" \
    -F body=@"$REPORT" >/dev/null 2>&1; then
  echo "posted a new comment"
else
  echo "::warning::Could not post the PR comment (token may lack pull-requests: write). \
The verdict is still in the job summary and the commit status."
fi
