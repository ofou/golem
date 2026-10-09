#!/usr/bin/env bash
set -euo pipefail

out() { printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"; }
deny() {
  out allowed false
  echo "Golem did not run: $*" | tee -a "$GITHUB_STEP_SUMMARY"
  exit 0
}
trim() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  printf '%s' "${text%"${text##*[![:space:]]}"}"
}

task="$INPUT_TASK"
sha="$GITHUB_SHA"
trusted=true
issue=""

if [ "$GITHUB_EVENT_NAME" = issue_comment ]; then
  [ "$EVENT_ACTION" = created ] || deny "only a new comment starts Golem, not an edited one."
  [ "$COMMENT_USER_TYPE" = User ] || deny "comments from bots and apps do not start Golem."
  [ "$COMMENT_USER" = "$GITHUB_ACTOR" ] || deny "the comment's author is not the actor of this run."
  first="${COMMENT_BODY%%$'\n'*}"
  first="${first%$'\r'}"
  case "$first" in "/golem" | "/golem "*) ;; *) deny "the comment does not start with /golem." ;; esac
  task="$(trim "${COMMENT_BODY#/golem}")"
  issue="$ISSUE_NUMBER"
fi

case "$GITHUB_EVENT_NAME" in
  workflow_dispatch | schedule | push) ;;
  *)
    permission="$(gh api "repos/$GITHUB_REPOSITORY/collaborators/$GITHUB_ACTOR/permission" --jq .permission 2>/dev/null)" ||
      deny "could not read $GITHUB_ACTOR's permission on $GITHUB_REPOSITORY."
    case "$permission" in
      admin | write) ;;
      *) deny "$GITHUB_ACTOR has $permission access; Golem runs only for people with write access." ;;
    esac
    ;;
esac

if [ -n "$ALLOWED_USERS" ]; then
  listed=false
  for user in $(tr ',' ' ' <<<"$ALLOWED_USERS"); do
    if [ "${user,,}" = "${GITHUB_ACTOR,,}" ]; then listed=true; fi
  done
  [ "$listed" = true ] || deny "$GITHUB_ACTOR is not in allowed-users."
fi

task="$(trim "$task")"
[ -n "$task" ] || deny "no task: write it after /golem, or pass the task input."
[ "${#task}" -le 20000 ] || deny "the task is ${#task} characters; the limit is 20000."

if [ "$IS_PULL_REQUEST" = true ]; then
  head="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$ISSUE_NUMBER" --jq '[.head.sha, (.head.repo.full_name // "")] | @tsv')" ||
    deny "could not read pull request #$ISSUE_NUMBER."
  sha="${head%%$'\t'*}"
  head_repo="${head#*$'\t'}"
  [ -n "$head_repo" ] || deny "the head repository of pull request #$ISSUE_NUMBER no longer exists."
  if [ "$head_repo" != "$GITHUB_REPOSITORY" ]; then trusted=false; fi
fi

licence=""
if [ -n "$INPUT_LICENCE" ]; then
  [[ "$INPUT_LICENCE" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ && "$INPUT_LICENCE" != *..* ]] ||
    deny "licence must be a plain relative path, not '$INPUT_LICENCE'."
  gh api -H "Accept: application/vnd.github.raw+json" \
    "repos/$GITHUB_REPOSITORY/contents/$INPUT_LICENCE?ref=$GITHUB_SHA" >"$RUNNER_TEMP/licence.json" ||
    deny "could not read $INPUT_LICENCE at $GITHUB_SHA."
  licence="$(base64 -w0 "$RUNNER_TEMP/licence.json")"
  echo "licence: $INPUT_LICENCE at $GITHUB_SHA, sha256 $(sha256sum "$RUNNER_TEMP/licence.json" | cut -d' ' -f1)"
fi

if [ -n "$COMMENT_ID" ]; then
  gh api -X POST "repos/$GITHUB_REPOSITORY/issues/comments/$COMMENT_ID/reactions" -f content=eyes >/dev/null ||
    echo "::notice title=Golem::could not react to the comment"
fi

delimiter="GOLEM_TASK_$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
{
  echo "task<<$delimiter"
  printf '%s\n' "$task"
  echo "$delimiter"
} >>"$GITHUB_OUTPUT"
out allowed true
out sha "$sha"
out trusted "$trusted"
out issue "$issue"
out licence "$licence"
if [ "$trusted" = true ]; then registry="shared"; else registry="read-only: the code is a fork's"; fi
echo "Golem runs for $GITHUB_ACTOR on $sha (registry $registry)." >>"$GITHUB_STEP_SUMMARY"
