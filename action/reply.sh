#!/usr/bin/env bash
set -euo pipefail

body="$RUNNER_TEMP/golem-reply.md"
answer="$ANSWER_DIR/run/result.md"
short="${SHA:0:12}"
run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"

case "$REGISTRY_STATUS" in
  saved) registry="saved for the next run" ;;
  unchanged) registry="unchanged" ;;
  read-only) registry="not saved: this run read a fork's code" ;;
  not-saved) registry="**not saved** (the Actions cache refused the write; see the run log)" ;;
  guarded) registry="**not saved**: no snapshot was restored although some exist (see the run log)" ;;
  "") registry="unknown" ;;
  *) registry="$REGISTRY_STATUS" ;;
esac
case "$VERIFY_RESULT" in
  success) verify="passed with no key on the runner" ;;
  failure) verify="**failed** (see the verify job)" ;;
  *) verify="${VERIFY_RESULT:-skipped}" ;;
esac

{
  echo "<!-- golem:run:$GITHUB_RUN_ID -->"
  if [ "$RUN_RESULT" = success ] && [ "$EXIT_CODE" = 0 ] && [ -f "$answer" ]; then
    echo "**Golem** answered on \`$short\` · [run]($run_url)"
  else
    echo "**Golem** did not finish (job ${RUN_RESULT:-not run}, exit ${EXIT_CODE:-none}) on \`$short\` · [run log]($run_url)"
  fi
  echo
  echo "_Written by language models through OpenRouter. Check it before you rely on it. Report a wrong or harmful answer at https://github.com/ofou/golem/issues._"
  echo
  echo "- tools installed: ${INSTALLED:-none}"
  echo "- spend: ${SPENT:-unknown}"
  echo "- registry: $registry"
  echo "- installed tools re-tested: $verify"
  if [ -f "$answer" ]; then
    echo
    python3 -I -c 'import sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
limit = 60000
print(text[:limit] + ("\n\n(cut at %d characters; the full answer is in the run artifact)" % limit if len(text) > limit else ""))' "$answer"
  fi
} >"$body"

gh api "repos/$GITHUB_REPOSITORY/issues/$ISSUE/comments" -F "body=@$body" --jq .html_url
