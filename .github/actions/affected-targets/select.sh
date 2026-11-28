#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# Chooses the Bazel test targets that the change from BASE_SHA to HEAD_SHA can
# affect, with target-determinator (TD). When the script can't decide, it
# selects the root and all shared rules modules. Every path ends in finish,
# which restores HEAD_SHA, verifies a clean checkout after running tools,
# and writes the outputs and a job summary. Without a checkout to verify,
# the script fails.
set -euo pipefail

# A directory of its own for each run, so a second run in the job leaves the
# first run's file alone.
out=$(mktemp -d "${RUNNER_TEMP:?}/affected-targets.XXXXXXXX")
file=$out/targets.txt
log=$out/target-determinator.log
changed=$out/changed
want='' ran=''

# Match the local rules modules in MODULE.bazel. Use the same scope and
# exclusions for discovery and the fallback pattern file.
patterns=(//... @tbhb_rules//... @tbhb_rules_ansible//... @tbhb_rules_astro//...
  @tbhb_rules_cloudflare//... @tbhb_rules_d2//... @tbhb_rules_go//...
  @tbhb_rules_playwright//... @tbhb_rules_python//... @tbhb_rules_storybook//...
  @tbhb_rules_structurizr//... @tbhb_rules_swift//... @tbhb_rules_tokens//...
  @tbhb_rules_typescript//...)
excluded=(@tbhb_rules//tools:mutmut_test @tbhb_rules_go//tests/self:gomutants_test
  @tbhb_rules_python//private:mutmut_test)

# die MESSAGE: fail the step, for a checkout the script can't verify or
# restore, or bazel-opts it can't split.
die() {
  echo "affected-targets: $*; stopping" >&2
  exit 1
}

# rand: 32 random hex digits, for a delimiter or token no input can guess.
rand() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

# show: the one way the script prints text that can hold a file name or a
# log from the change. Each line of stdin gets a prefix and a newline, with
# workflow commands stopped, so neither form of command and no unfinished
# last line can reach the runner or swallow the resume token.
show() {
  local token
  token=$(rand)
  echo "::stop-commands::$token"
  awk '{ print "  | " $0 }' || true
  echo "::$token::"
}

# clean: the tree and every submodule hold no change, untracked files
# included whatever status.showUntrackedFiles says, and no index entry is
# marked assume-unchanged (a lowercase tag) or skip-worktree (`S`), which
# hides a change from git status. Git's errors go nowhere, since a
# submodule's path can hold a workflow command.
clean() {
  local changes flags
  changes=$(git status --porcelain --untracked-files=all --ignore-submodules=none 2>/dev/null) &&
    [[ -z $changes ]] && flags=$(git ls-files -v --recurse-submodules :/ 2>/dev/null) &&
    ! grep -q '^[[:lower:]S]' <<<"$flags"
}

# at_head: HEAD is the event's head and, once a tool has run, the checkout is
# clean, since a stopped checkout can leave the base's content under the head.
at_head() {
  local now
  now=$(git rev-parse HEAD 2>/dev/null) && [[ $now == "$want" ]] || return
  [[ -z $ran ]] || clean
}

# ensure_head: put the checkout back on the commit the event tests, by force
# once a tool has run. Git's messages go through show; pipefail keeps its
# status.
ensure_head() {
  ! at_head || return 0
  echo "affected-targets: checking out the event's head $want" >&2
  git checkout -q ${ran:+-f} --detach "$want" 2>&1 | show >&2 || return
  [[ -z $ran ]] || git submodule update -q --recursive --force 2>&1 | show >&2 || return
  at_head
}

# finish MODE REASON: MODE is `all` or `selected`. REASON can hold a changed
# path, so it loses control characters here and Markdown in the summary. A
# selection holds only for the clean head the tools analyzed, so finish
# never repairs the checkout under one and falls back instead.
finish() {
  [[ $1 == all ]] || at_head || set -- all 'the checkout changed while the tools ran'
  local all=false count line reason=${2//[[:cntrl:]]/?} delim
  ensure_head || die "can't restore a clean checkout of the event's head $want"
  if [[ $1 == all ]]; then
    all=true count=all line="Running every target in the root and shared rules modules: "
    printf '%s\n' "${patterns[@]}" "${excluded[@]/#/-}" >"$file"
  else
    count=$(grep -c . "$file" || true) line="Selected $count test targets: "
  fi
  # A random delimiter, which no target line can match by chance.
  delim=EOF_$(rand)
  {
    echo "all=$all"
    echo "count=$count"
    echo "reason=$reason"
    echo "file=$file"
    echo "targets<<$delim"
    cat "$file"
    echo "$delim"
  } >>"${GITHUB_OUTPUT:?}"
  line+=$(printf '%s' "$reason" | LC_ALL=C sed 's/[[:punct:]]/\\&/g')
  printf '### Affected Bazel targets\n\n%s\n' "$line" >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
  echo "affected-targets: $count: $reason" | show
  exit 0
}

# deadline SECONDS COMMAND...: run COMMAND in a process group of its own,
# with status 124 like timeout(1) when it runs past SECONDS. The group gets
# TERM at the deadline, KILL 60 seconds later, and KILL once COMMAND exits,
# so no child it left running can check out the base after finish restores
# the event's head. The watchdog has a group of its own too, so one kill
# stops it and its sleep.
deadline() {
  local pid dog status=0 _
  rm -f "$out/expired"
  set -m
  "${@:2}" </dev/null &
  pid=$!
  (sleep "$1" && : >"$out/expired" && kill -TERM -- "-$pid" && sleep 60 && kill -KILL -- "-$pid") \
    </dev/null >/dev/null 2>&1 &
  dog=$!
  set +m
  { wait "$pid"; } 2>/dev/null || status=$?
  kill -KILL -- "-$dog" "-$pid" 2>/dev/null || true
  wait "$dog" 2>/dev/null || true
  for _ in {1..50}; do
    kill -0 -- "-$pid" 2>/dev/null || break
    sleep 0.1
  done
  [[ ! -e $out/expired ]] || status=124
  return "$status"
}

head=$(git rev-parse HEAD 2>/dev/null) || die "no Git checkout, so the event's head can't be verified"
[[ ${BAZEL_OPTS:-} != *$'\n'* ]] || die 'bazel-opts must be one line'
want=${HEAD_SHA:-$head}
[[ -n ${HEAD_SHA:-} ]] || finish all "no event head to check the checkout $head against"

# Any other event, such as push or schedule, tests everything.
[[ ${EVENT:-} == pull_request || ${EVENT:-} == merge_group || ${EVENT:-} == workflow_dispatch ]] ||
  finish all "a '${EVENT:-}' event tests everything"
[[ $head == "$want" ]] || finish all "the checkout $head is not the event's head $want"
[[ ${QUERY:-} != *$'\n'* ]] || finish all 'the query is not one line'
if [[ ${EVENT:-} == workflow_dispatch ]]; then
  dispatch_base=${BASE_SHA:-HEAD^}
  BASE_SHA=$(git rev-parse --verify --end-of-options "${dispatch_base}^{commit}" 2>/dev/null) ||
    finish all "the dispatch base '$dispatch_base' is missing from the checkout (check out with fetch-depth: 0)"
fi
git cat-file -e "${BASE_SHA:-}^{commit}" 2>/dev/null ||
  finish all "the base '${BASE_SHA:-}' is missing from the checkout (check out with fetch-depth: 0)"
git -c core.quotePath=false diff -z --raw --no-renames --no-relative --ignore-submodules=none --output="$changed" "$BASE_SHA" "$head" 2>&1 | show >&2 ||
  finish all 'git diff failed'

# Target-determinator hashes the configured graph. Submodule and symlink
# changes can affect files outside the paths in the diff. Module and Bazel
# configuration changes can invalidate the whole graph.
# A newline-separated set of labels, as bash 3.2 on macOS has no associative arrays.
workspace_checks=
workspace_check_count=0
add_workspace_check() {
  case $'\n'$workspace_checks in
  *$'\n'"$1"$'\n'*) ;;
  *)
    workspace_checks+=$1$'\n'
    workspace_check_count=$((workspace_check_count + 1))
    ;;
  esac
}
# Gazelle walks the checkout for sources, BUILD directives, and embedded
# files. Embeds can have any extension.
if [[ -s $changed ]]; then
  add_workspace_check //:gazelle_test
fi
while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
  [[ ${meta:0:15} != *160000* ]] || finish all "the submodule $path changed"
  [[ ${meta:0:15} != *120000* ]] || finish all "the symlink $path changed"
  case ${path##*/} in
  MODULE.bazel* | *.MODULE.bazel | WORKSPACE* | REPO.bazel | \
    .bazelrc* | *bazelrc | .bazelversion | .bazeliskrc | .bazelignore)
    finish all "$path changed"
    ;;
  BUILD | BUILD.* | *.BUILD | *.bazel | *.bzl | *.sky | *.star)
    # The root buildifier excludes lib/rules, which has its own scan.
    if [[ $path == lib/rules/* ]]; then
      add_workspace_check @tbhb_rules//:buildifier_test
    else
      add_workspace_check //:buildifier_test
    fi
    ;;
  esac
  case $path in
  lib/rules/*.sh | lib/rules/*.sh.tpl | lib/rules/*.yml)
    add_workspace_check @tbhb_rules//repo/tests:mktemp_test
    ;;
  esac
  # Markdown links can point to any tracked file or directory in lib/rules.
  if [[ $path == lib/rules/* && (${meta: -1} != M || $path == *.md || $path == lib/rules/.markdown-links.toml) ]]; then
    add_workspace_check @tbhb_rules//repo/tests:markdown_links_test
  fi
  case $path in
  lib/rules/.gitleaks*.toml | lib/rules/*/.gitleaks*.toml | lib/rules/lint/gitleaks/* | \
    lib/rules/pins/extensions.bzl | LICENSES/MIT.txt)
    add_workspace_check @tbhb_rules//lint/tests:gitleaks_configs_test
    ;;
  esac
done <"$changed"

[[ -n ${TD:-} ]] || finish all 'target-determinator is unavailable on this runner'
minutes=${TIMEOUT_MINUTES:-15}
[[ $minutes =~ ^[1-9][0-9]*$ ]] || finish all "timeout-minutes '$minutes' is not a whole number"
# Tests set TIMEOUT_SECONDS to stop a fake run sooner.
seconds=${TIMEOUT_SECONDS:-$((minutes * 60))}

# target-determinator would analyze a change on top of the head as the head.
clean || finish all "the checkout of $head has changes or flagged index entries, or git failed"

read -ra opts <<<"${BAZEL_OPTS:-}"
opts+=(--skip_incompatible_explicit_targets)
# Repository rules run code from the change, so Bazel gets no runner
# command files to write outputs, the summary or the environment through.
isolate=(env -u GITHUB_OUTPUT -u GITHUB_STEP_SUMMARY -u GITHUB_ENV -u GITHUB_PATH -u GITHUB_STATE)
start=$SECONDS ran=1

run_step() {
  step=$1
  shift
  local left=$((seconds + start - SECONDS))
  ((left > 0)) || return 124
  deadline "$left" "${isolate[@]}" "$@" 2>>"$log"
}

label_set() {
  jq -Rrs 'split("\n") | map(select(length > 0) | @json) | "set(" + join(" ") + ")"' "$1"
}

# Bazel renders list attributes with brackets and a comma-space separator.
# Confirm matches against the actual list, since tags can contain that separator.
manual='(^\[|, )manual(, |\]$)'
manual_tags='any(.attribute[]; .name == "tags" and (.stringListValue | index("manual") != null))'
query_candidates() {
  local dir=$1 scope=$2 excluded_manual candidates
  shift 2
  local -a query=(bash -c 'cd -- "$1" && shift && exec "$@"' _ "$dir" bazelisk "$@" query)
  run_step "$step" "${query[@]}" --output=streamed_jsonproto \
    "attr(tags, '$manual', kind('.*_test rule|test_suite rule', $scope))" >"$out/manual.json" || return
  excluded_manual=$(jq -rs "map(.rule | select($manual_tags) | .name | @json) | \"set(\" + join(\" \") + \")\"" \
    "$out/manual.json" 2>>"$log") || return
  # Non-manual suites can still include manual tests. Strict expansion makes
  # invalid suite members fail the query instead of silently dropping them.
  # The nightly workflow runs the CLI mutation test explicitly.
  candidates="let suites = kind('test_suite rule', $scope) in
    ((kind('.*_test rule', $scope) except $excluded_manual)
    + tests(\$suites except $excluded_manual)) except set(${excluded[*]})
    except set(//factory/cli:mutmut_test)"
  [[ -z ${QUERY:-} ]] || candidates="($candidates) intersect ($QUERY)"
  run_step "$step" "${query[@]}" --strict_test_suite --output=label "$candidates"
}

check_base_candidates() {
  local scope=$1 prefix status=0
  step='locating the Bazel workspace'
  prefix=$(git rev-parse --show-prefix 2>>"$log") || return
  run_step 'creating the base worktree' git worktree add --quiet --detach "$out/base" "$BASE_SHA" >/dev/null || return
  step='the query of base candidate tests'
  query_candidates "$out/base/$prefix" "$scope" --batch >"$out/base-candidates" || status=$?
  git worktree remove --force "$out/base" >>"$log" 2>&1 || {
    step='removing the base worktree'
    return 1
  }
  ((status == 0)) || return "$status"

  # An unchanged test may be newly runnable through a suite. Hashing its
  # rule and dependencies at both revisions would miss that change.
  step='comparing candidate tests'
  LC_ALL=C sort -u -o "$out/base-candidates" "$out/base-candidates" || return
  LC_ALL=C sort -u -o "$out/candidates" "$out/candidates" || return
  LC_ALL=C comm -13 "$out/base-candidates" "$out/candidates" >"$out/new-candidates" || return
  [[ ! -s $out/new-candidates ]] || finish all 'the candidate query found tests newly runnable at the head'
}

select_targets() {
  local scope targets td_output=$file
  local -a td_flags=()
  scope=$(
    IFS=+
    printf '(%s)' "${patterns[*]}"
  )
  # Resolve the wildcard expansion before cquery, which would analyze every
  # matched rule, including intentionally failing manual proof targets.
  step='the query of candidate tests'
  query_candidates . "$scope" >"$out/candidates" || return
  if [[ -s $out/candidates ]]; then
    check_base_candidates "$scope" || return
    targets=$(label_set "$out/candidates") || return

    # The determinator hashes configured dependencies in local overrides too.
    if [[ ${EXPLAIN:-} == true ]]; then
      td_flags=(-verbose)
      td_output=$out/explanations
    fi
    run_step target-determinator "$TD" ${td_flags[@]+"${td_flags[@]}"} -bazel bazelisk \
      -nocache_results -delete-cached-worktree -before-query-error-behavior=fatal \
      -targets "$targets" "${opts[@]/#/-bazel-opts=}" "$BASE_SHA" >"$td_output" || return
    if [[ ${EXPLAIN:-} == true ]]; then
      # Verbose stdout includes optional reasons and can repeat a label for different configurations.
      step='reading target-determinator explanations'
      cat "$td_output" >>"$log" || return
      LC_ALL=C sed 's/ Changes:.*//' "$td_output" | LC_ALL=C sort -u >"$file" || return
    fi
  fi
  if ((workspace_check_count)); then
    step='the query of workspace checks'
    newline=$'\n' # bash 3.2 doesn't expand $'' inside a quoted pattern
    run_step "$step" bazelisk query --consistent_labels --output=label \
      "kind('.*_test rule', set(${workspace_checks//"$newline"/ }))" >"$out/workspace-checks" || return
    [[ $(wc -l <"$out/workspace-checks") -eq $workspace_check_count ]] || return 1
    # Bazel can spell the main repository with or without an explicit @@.
    step='combining affected tests and workspace checks'
    LC_ALL=C sed 's,^@@//,//,' "$file" "$out/workspace-checks" | LC_ALL=C sort -u >"$out/combined" || return
    mv "$out/combined" "$file" || return
  fi
  [[ -s $file ]] || return 0
  targets=$(label_set "$file") || return
  run_step 'the cquery of manual tags' bazelisk cquery --output=jsonproto "${opts[@]}" \
    "attr(tags, '$manual', $targets)" >"$out/tags.json" || return
  jq -es "length == 1 and (.[0] | type == \"object\" and ((.results // []) |
    type == \"array\" and all(.[]; .target.rule | ($manual_tags | not))))" "$out/tags.json" >/dev/null 2>>"$log" ||
    finish all 'an affected test is tagged manual, or its tags are unreadable'
}

: >"$file"
: >"$log"
status=0
select_targets || status=$?
[[ $status != 0 ]] || finish selected "target-determinator compared $BASE_SHA with $head"
# The log comes from the change too.
tail -n 40 "$log" | show >&2
[[ $status != 124 ]] || finish all "$step timed out after $seconds seconds"
finish all "$step failed with exit status $status"
