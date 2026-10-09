#!/usr/bin/env bash
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CACHE_DIR="${RUNNER_TEMP:?}/golem-registry"
KEY_PREFIX="golem-registry-v1-"

fail() {
  echo "::error title=Golem::$*"
  exit 1
}
out() { printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"; }
remember() { printf '%s=%q\n' "$1" "$2" >>"$GOLEM_STATE/env"; }
load() {
  # shellcheck source=/dev/null
  . "$GOLEM_STATE/env"
}
golem() {
  (cd "$GOLEM_STATE" && PYTHONPATH="$ROOT" TMPDIR="$GOLEM_STATE/tmp" \
    "$GOLEM_STATE/venv/bin/python" -P -m golem --repo "$repo" --licence "$lic" "$@")
}
py() { "$GOLEM_STATE/venv/bin/python" -I "$@"; }
digest() {
  (
    cd "$1" 2>/dev/null || exit 0
    find registry usage.jsonl gaps.jsonl -type f -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 -r sha256sum
  ) | sha256sum | cut -d' ' -f1
}
keep_journals() {
  for journal in usage gaps; do
    if [ -f "$1/$journal.jsonl" ]; then cp "$1/$journal.jsonl" "$2/$journal.jsonl"; fi
  done
}
fence() {
  echo '`````'
  cut -c1-240 "$1"
  echo '`````'
}

setup() {
  [ "${RUNNER_OS:-}" = Linux ] || fail "Golem needs a Linux runner with Docker; this one is ${RUNNER_OS:-unknown}."
  case "$GOLEM_COMMAND" in run | verify) ;; *) fail "command must be run or verify, not '$GOLEM_COMMAND'." ;; esac
  case "$GOLEM_REGISTRY" in read-write | read-only | off) ;; *) fail "registry must be read-write, read-only or off, not '$GOLEM_REGISTRY'." ;; esac
  cd "$GITHUB_WORKSPACE"
  [ -d "$GOLEM_PATH" ] || fail "path '$GOLEM_PATH' is not a directory."
  local repo lic state line file
  repo="$(cd "$GOLEM_PATH" && pwd -P)"
  case "$repo" in *:*) fail "Docker cannot mount a path that contains ':' ($repo)." ;; esac
  if [ "$GOLEM_COMMAND" = run ]; then
    [ -n "$GOLEM_TASK" ] || fail "task is empty."
    [ "$GOLEM_HAS_KEY" = true ] || fail "openrouter-api-key is empty. Add the repository secret OPENROUTER_API_KEY (a key with its own credit limit) and pass it as openrouter-api-key."
  fi
  docker info >/dev/null 2>&1 || fail "Docker is not running. Golem runs generated code only in its sandbox containers."
  if [ -n "$GOLEM_LICENCE" ]; then
    lic="$(cd "$GITHUB_WORKSPACE" && realpath -e -- "$GOLEM_LICENCE" 2>/dev/null)" || fail "licence file '$GOLEM_LICENCE' not found."
  else
    lic="$ROOT/authority.json"
  fi
  state="$(mktemp -d "$RUNNER_TEMP/golem.XXXXXX")"
  chmod 755 "$state"
  mkdir -p "$state/tmp" "$state/out"
  : >"$state/attach"
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] || continue
    file="$(cd "$GITHUB_WORKSPACE" && realpath -e -- "$line" 2>/dev/null)" || fail "attachment '$line' not found."
    [ -f "$file" ] || fail "attachment '$line' is not a file."
    printf '%s\n' "$file" >>"$state/attach"
  done <<<"$GOLEM_ATTACH"
  GOLEM_STATE="$state"
  remember repo "$repo"
  remember lic "$lic"
  rm -rf "$CACHE_DIR"
  out state "$state"
}

install() {
  load
  [ -x "${GOLEM_PYTHON:-}" ] || fail "actions/setup-python returned no interpreter."
  "$GOLEM_PYTHON" -m venv "$GOLEM_STATE/venv"
  "$GOLEM_STATE/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-input \
    --require-hashes -r "$ROOT/requirements.lock"
  echo "licence: $(golem licence | tail -1)"
  local image attempt
  image="$(py -c 'import json, sys
lines = [l for l in open(sys.argv[1], encoding="utf-8") if not l.lstrip().startswith("//")]
print(json.loads("".join(lines))["sandbox"]["image"])' "$lic")"
  [[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]] || fail "the licence's sandbox image '$image' is not an image reference."
  for attempt in 1 2 3; do
    if docker pull -q "$image" >/dev/null; then break; fi
    [ "$attempt" -lt 3 ] || fail "could not pull the sandbox image $image."
    sleep $((attempt * 10))
  done
  echo "sandbox image: $(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || echo "$image")"
}

place() {
  load
  if [ "$GOLEM_REGISTRY" != off ] && [ -n "${GOLEM_MATCHED_KEY:-}" ] && [ -d "$CACHE_DIR/registry" ]; then
    mkdir -p "$repo/.golem"
    rm -rf "$repo/.golem/registry"
    cp -R "$CACHE_DIR/registry" "$repo/.golem/registry"
    keep_journals "$CACHE_DIR" "$repo/.golem"
    echo "registry: restored snapshot $GOLEM_MATCHED_KEY"
  elif [ "$GOLEM_REGISTRY" != off ]; then
    echo "registry: no snapshot in the Actions cache; starting from the checkout"
  fi
  remember before "$(digest "$repo/.golem")"
  (ls -1 "$repo/.golem/runs" 2>/dev/null || true) | LC_ALL=C sort >"$GOLEM_STATE/runs.before"
  golem registry || true
}

attachments() {
  local file
  while IFS= read -r file; do args+=(--attach "$file"); done <"$GOLEM_STATE/attach"
}

run() {
  load
  local -a args=(run)
  attachments
  args+=(-- "$GOLEM_TASK")
  local code
  set +e
  golem "${args[@]}" 2>&1 | tee "$GOLEM_STATE/out/session.log"
  code=${PIPESTATUS[0]}
  set -e
  remember code "$code"
  out exit-code "$code"
  [ "$code" = 0 ] || echo "::error title=Golem::golem run exited $code; see the log above."
}

verify() {
  load
  local -a args=(verify)
  attachments
  local code
  set +e
  golem "${args[@]}" 2>&1 | tee "$GOLEM_STATE/out/verify.log"
  code=${PIPESTATUS[0]}
  set -e
  remember code "$code"
  out exit-code "$code"
  {
    echo "### Golem verify (no OpenRouter key in this step)"
    fence "$GOLEM_STATE/out/verify.log"
  } >>"$GITHUB_STEP_SUMMARY"
}

collect() {
  load
  local run_id="" dir="" parsed installed="" spent=""
  run_id="$( (ls -1 "$repo/.golem/runs" 2>/dev/null || true) | LC_ALL=C sort | LC_ALL=C comm -13 "$GOLEM_STATE/runs.before" - | tail -1)"
  if [ -n "$run_id" ]; then dir="$repo/.golem/runs/$run_id"; fi
  if [ -n "$dir" ] && [ -f "$dir/events.jsonl" ]; then
    parsed="$(py -c 'import json, sys
installed, spent = [], ""
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("kind") == "install":
        installed.append(event["text"].split()[0])
    elif event.get("kind") == "spend":
        spent = event["text"].split()[0]
print(" ".join(installed))
print(spent)' "$dir/events.jsonl")"
    installed="$(sed -n 1p <<<"$parsed")"
    spent="$(sed -n 2p <<<"$parsed")"
  else
    echo "::warning title=Golem::the run left no events in .golem/runs"
  fi
  remember run_id "$run_id"
  out run-id "$run_id"
  out installed "$installed"
  out spent "$spent"
  if [ -n "$dir" ] && [ -f "$dir/result.md" ]; then out answer-file "$dir/result.md"; fi
  {
    echo "### Golem"
    echo
    echo "_Written by language models through OpenRouter. Check it before you rely on it. Report a wrong or harmful answer at https://github.com/ofou/golem/issues._"
    echo
    echo "- task: $(head -c 300 <<<"$GOLEM_TASK" | tr '\n' ' ')"
    echo "- exit code: ${code:-none}"
    echo "- installed this run: ${installed:-none}"
    echo "- spend: ${spent:-unknown}"
    echo
    echo "<details><summary>Session (kernel lines)</summary>"
    echo
    grep -E '^\[(licence|registry|session|create|jev|verdict|install|respawn|call|spend|refused|dispute)\]' \
      "$GOLEM_STATE/out/session.log" >"$GOLEM_STATE/summary.log" || true
    fence "$GOLEM_STATE/summary.log"
    echo
    echo "</details>"
    echo
    if [ -n "$dir" ] && [ -f "$dir/result.md" ]; then
      echo "#### Answer"
      echo
      head -c 60000 "$dir/result.md"
    fi
  } >>"$GITHUB_STEP_SUMMARY"
}

stage() {
  load
  local status=""
  if [ -z "${code:-}" ]; then
    status="no-run"
  elif [ ! -d "$repo/.golem/registry" ]; then
    status="empty"
  elif [ "$(digest "$repo/.golem")" = "${before:-}" ]; then
    status="unchanged"
  elif [ -z "${GOLEM_MATCHED_KEY:-}" ] && guarded; then
    status="guarded"
  fi
  if [ -n "$status" ]; then
    remember registry_status "$status"
    out save false
    return
  fi
  rm -rf "$CACHE_DIR"
  mkdir -p "$CACHE_DIR"
  cp -R "$repo/.golem/registry" "$CACHE_DIR/registry"
  find "$CACHE_DIR/registry" -mindepth 2 -maxdepth 2 -type d -name '.*' -exec rm -rf {} +
  keep_journals "$repo/.golem" "$CACHE_DIR"
  out save true
  out key "$KEY_PREFIX$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$(date +%s)"
}

guarded() {
  local listing count
  listing="$(gh api "repos/$GITHUB_REPOSITORY/actions/caches?key=$KEY_PREFIX&per_page=100" 2>/dev/null)" || {
    echo "::notice title=Golem::could not list registry snapshots (the token needs actions: read); saving without that check"
    return 1
  }
  count="$(py -c 'import json, sys
refs = {sys.argv[1], "refs/heads/" + sys.argv[2]}
print(sum(1 for c in json.loads(sys.stdin.read())["actions_caches"] if c["ref"] in refs))' \
    "$GITHUB_REF" "${GOLEM_DEFAULT_BRANCH:-}" <<<"$listing")"
  if [ "$count" -gt 0 ]; then
    echo "::warning title=Golem::No registry snapshot was restored, but this repository has $count. Not saving this run's registry, so it does not hide them. If this repeats, delete them: gh cache delete --all"
    return 0
  fi
  return 1
}

report() {
  load
  local status="${registry_status:-}"
  case "$GOLEM_REGISTRY" in
    off | read-only) status="$GOLEM_REGISTRY" ;;
    *)
      if [ "${GOLEM_SAVE:-}" = true ]; then
        if [ "${GOLEM_SAVED_HIT:-}" = true ]; then
          status="saved"
          echo "registry: saved snapshot $GOLEM_KEY"
        else
          status="not-saved"
          echo "::warning title=Golem::The registry snapshot was not saved, so tools installed by this run will be missing next time. issue_comment, pull_request_target and workflow_run runs get a read-only Actions cache unless the job sets cache-mode: write."
        fi
      fi
      ;;
  esac
  out registry "${status:-unknown}"
}

package() {
  load
  local dir="$GOLEM_STATE/out" leaked
  if [ -d "$repo/.golem/registry" ]; then
    cp -R "$repo/.golem/registry" "$dir/registry"
    keep_journals "$repo/.golem" "$dir"
  fi
  if [ -n "${run_id:-}" ] && [ -d "$repo/.golem/runs/$run_id" ]; then cp -R "$repo/.golem/runs/$run_id" "$dir/run"; fi
  if [ -n "${OPENROUTER_API_KEY:-}" ]; then
    while IFS= read -r leaked; do
      rm -f -- "$leaked"
      echo "::warning title=Golem::left ${leaked#"$dir"/} out of the artifact: it contained the OpenRouter key"
    done < <(grep -rlF -- "$OPENROUTER_API_KEY" "$dir" || true)
  fi
  out name "$GOLEM_ARTIFACT-${run_id:-$GITHUB_RUN_ID}"
  out dir "$dir"
}

finish() {
  load
  [ -n "${code:-}" ] || fail "Golem did not run."
  exit "$code"
}

step="${1:-}"
case "$step" in
  setup | install | place | run | verify | collect | stage | report | package | finish) "$step" ;;
  *) fail "unknown step '$step'" ;;
esac
