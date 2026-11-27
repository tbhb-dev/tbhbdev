#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# The affected-targets action's selection script, against a scratch Git
# repository and a fake target-determinator. Every case must end with
# outputs and a summary line, each undecidable one must select all patterns, and
# every case must leave the checkout at the event's head.

set -euo pipefail
action=${TEST_SRCDIR:?}/${ACTION_RLOCATIONPATH:?}
script=${action%/*}/select.sh
git_bin=${TEST_SRCDIR:?}/${GIT_RLOCATIONPATH:?}
scratch=${TEST_TMPDIR:?}/affected
mkdir -p "$scratch/bin" "$scratch/tmp" "$scratch/repo/sub" "$scratch/no-git" "$scratch/broken"
ln -s "$git_bin" "$scratch/bin/git"
ln -s "$scratch/fake-td" "$scratch/bin/bazelisk"
# One fake serves as target-determinator and as bazelisk. Each records its
# arguments and which runner command files it can see. `fail` and `hang`
# first check out the base, as a stopped run could leave it, and start a
# child that checks out the base again later unless it's stopped too.
# `mixed` leaves the base's content and an older submodule commit under the
# head, `litter` an untracked file, `smudge` a change after a selection, and
# `bare` a last log line with no newline.
# The loading query lists candidates. The cquery reports manual tests only
# when their labels appear in the query of selected targets.
cat >"$scratch/fake-td" <<'MOCK'
#!/usr/bin/env bash
matches_tag() {
  local regex=${2#*attr(tags, \'}
  regex=${regex%%\'*}
  [[ "[$1]" =~ $regex ]]
}
for name in GITHUB_OUTPUT GITHUB_STEP_SUMMARY GITHUB_ENV GITHUB_PATH GITHUB_STATE; do
  [[ -z ${!name+set} ]] || echo "$name" >>"$ENV_LOG"
done
if [[ ${0##*/} == bazelisk ]]; then
  [[ $1 != --batch ]] || shift
  if [[ $1 == query ]]; then
    if [[ $2 == --consistent_labels ]]; then
      [[ ${FAKE_WORKSPACE:-} != empty ]] || exit 0
      labels=${!#}
      labels=${labels#*set(}
      labels=${labels%))}
      for label in $labels; do
        case $label in
          //:*) echo "@@$label" ;;
          @tbhb_rules//*) echo "@@tbhb_rules+//${label#*//}" ;;
          *) exit 9 ;;
        esac
      done
      exit 0
    fi
    printf '%s\n' "$@" >"$QUERY_LOG"
    [[ $FAKE_QUERY != fail ]] || { echo 'ERROR: loading failed' >&2 && exit 6; }
    [[ $FAKE_QUERY != empty ]] || exit 0
    if [[ $2 == --output=streamed_jsonproto ]]; then
      if [[ $FAKE_QUERY == near_manual ]] && matches_tag "$FAKE_TAG" "${!#}"; then
        printf '{"rule":{"name":"//go:near_manual_test","attribute":[{"name":"tags","stringListValue":["%s"]}]}}\n' "$FAKE_TAG"
      fi
      exit 0
    fi
    if [[ $FAKE_QUERY == near_manual && ${!#} != *'attr(tags,'* && ${!#} != *//go:near_manual_test* ]]; then
      echo //go:near_manual_test
    fi
    if [[ $FAKE_QUERY == suite ]]; then
      suite=$(git show HEAD:lib/rules/diagrams/d2/BUILD.bazel)
      if [[ $suite == *':not_test'* && $2 == --strict_test_suite ]]; then
        echo 'ERROR: suite member does not refer to a test or test_suite rule' >&2
        exit 6
      fi
      if [[ $suite == *':manual_test'* ]]; then
        echo @tbhb_rules_d2//private:manual_test
      fi
      exit 0
    fi
    printf '%s\n' //go:{cmd,lib,manual}_test @tbhb_rules_d2//private:inputs_test \
      @tbhb_rules_structurizr//private:{d2,manual}_test //diagrams:integration_test
    if [[ $FAKE == cli_leaf && ${!#} != *'except set(//factory/cli:mutmut_test)'* ]]; then
      echo //factory/cli:mutmut_test
    fi
    exit
  fi
  printf '%s\n' "$@" >"$CQUERY_LOG"
  [[ $FAKE_CQUERY != fail ]] || { echo 'ERROR: analysis failed' >&2 && exit 7; }
  case ${!#} in
    *:manual_test*) tag=manual ;;
    *:near_manual_test*) if matches_tag "$FAKE_TAG" "${!#}"; then tag=$FAKE_TAG; else echo '{}'; fi ;;
    *) echo '{}' ;;
  esac
  if [[ -n ${tag:-} ]]; then
    printf '{"results":[{"target":{"rule":{"attribute":[{"name":"tags","stringListValue":["%s"]}]}}}]}\n' "$tag"
  fi
  exit
fi
printf '%s\n' "$@" >"$ARGS_LOG"
straggle() { (sleep 2 && git checkout -q "$1" && : >"$CHILD_MARK") </dev/null >/dev/null 2>&1 & }
case $FAKE in
  two) printf '//go:lib_test\n//go:cmd_test\n' ;;
  two_and_buildifier) printf '//go:lib_test\n//go:cmd_test\n//:buildifier_test\n' ;;
  tagged) printf '//go:lib_test\n//go:manual_test\n//go:cmd_test\n' ;;
  cli_leaf)
    [[ $(git diff --name-only "${!#}" HEAD) == factory/cli/src/tbhbdev/alpha/repo_token.py ]] || exit 8
    printf '//factory/cli:%s_test\n' bandit complexipy deptry pip_audit pylint pyrefly ruff vulture
    printf '//factory/cli/tests:test_alpha_%s\n' agent_invariants repo_token
    if [[ $* == *'"//factory/cli:mutmut_test"'* ]]; then
      printf '//factory/cli:mutmut_test\n'
    fi
    ;;
  near_manual) [[ $* != *//go:near_manual_test* ]] || echo //go:near_manual_test ;;
  rules)
    printf '%s\n' '@@tbhb_rules+//:buildifier_test' '@@tbhb_rules_d2+//private:inputs_test' \
      '@@tbhb_rules_structurizr+//private:d2_test' //diagrams:integration_test ;;
  rules_tagged) echo '@@tbhb_rules_structurizr+//private:manual_test' ;;
  space) printf '//a:with space\n//b:t\n' ;;
  none) ;;
  dispatch)
    for source in lib cmd; do
      if ! git diff --quiet "${!#}" HEAD -- ":/src/$source.go"; then
        echo "//go:${source}_test"
      fi
    done
    ;;
  fail) git checkout -q "${!#}" && straggle "${!#}" && echo 'ERROR: cquery failed' >&2 && exit 3 ;;
  hang) git checkout -q "${!#}" && straggle "${!#}" && exec sleep 30 ;;
  mixed) git -C ../lib checkout -q HEAD~ && git checkout -q "${!#}" -- :/ && exit 3 ;;
  litter) : >stray && exit 3 ;;
  smudge) echo x >>../README.md && echo //go:lib_test ;;
  bare) printf 'ERROR: no newline' >&2 && exit 3 ;;
esac
MOCK
# This git fails the subcommand BREAK names, with a workflow command in its error.
cat >"$scratch/broken/git" <<MOCK
#!/usr/bin/env bash
for arg in "\$@"; do [[ \$arg != "\$BREAK" ]] || { printf 'x\n##[notice]y\n' >&2 && exit 128; }; done
exec "$git_bin" "\$@"
MOCK
chmod +x "$scratch/fake-td" "$scratch/broken/git"
export PATH="$scratch/bin:$PATH" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_CEILING_DIRECTORIES=$scratch GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always
export RUNNER_TEMP=$scratch/tmp ARGS_LOG=$scratch/args ENV_LOG=$scratch/env TD=$scratch/fake-td
export QUERY_LOG=$scratch/query CQUERY_LOG=$scratch/cquery CHILD_MARK=$scratch/child-ran
export QUERY='' FAKE_QUERY='' FAKE_CQUERY='' BAZEL_OPTS='--config=ci --keep_going'
export GITHUB_OUTPUT=$scratch/output GITHUB_STEP_SUMMARY=$scratch/summary
export GITHUB_ENV=$scratch/github-env GITHUB_PATH=$scratch/github-path GITHUB_STATE=$scratch/state

fallback=$(
  cat <<'PATTERNS'
//...
@tbhb_rules//...
@tbhb_rules_ansible//...
@tbhb_rules_astro//...
@tbhb_rules_cloudflare//...
@tbhb_rules_d2//...
@tbhb_rules_go//...
@tbhb_rules_playwright//...
@tbhb_rules_python//...
@tbhb_rules_storybook//...
@tbhb_rules_structurizr//...
@tbhb_rules_swift//...
@tbhb_rules_tokens//...
@tbhb_rules_typescript//...
-@tbhb_rules//tools:mutmut_test
-@tbhb_rules_go//tests/self:gomutants_test
-@tbhb_rules_python//private:mutmut_test
PATTERNS
)

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# output KEY: KEY's value in GITHUB_OUTPUT, in either the one-line or the
# delimited form.
output() {
  awk -v key="$1" '
    delim != "" { if ($0 == delim) delim = ""; else if (mine) print; next }
    index($0, key "=") == 1 { print substr($0, length(key) + 2); next }
    (at = index($0, "<<")) && substr($0, 1, at - 1) ~ /^[A-Za-z_]+$/ {
      mine = substr($0, 1, at - 1) == key
      delim = substr($0, at + 2)
    }
  ' "$GITHUB_OUTPUT"
}

# stopped FILE: every line of FILE with `::` or the legacy `##[` in it, where
# the runner can see a workflow command, sits between ::stop-commands::TOKEN
# and ::TOKEN::.
stopped() {
  awk '
    tok == "" && index($0, "::stop-commands::") == 1 { tok = substr($0, 18); next }
    tok != "" { if ($0 == "::" tok "::") tok = ""; next }
    /::|##\[/ { bad = 1; print "unstopped: " $0 > "/dev/stderr" }
    END { exit bad || tok != "" }
  ' "$1"
}

cd "$scratch/repo"
git init -q -b main .
commit() {
  for path in "$@"; do
    mkdir -p "$(dirname "$path")"
    echo "$RANDOM" >>"$path"
  done
  git add -A
  git -c user.name=test -c user.email=test@example.invalid \
    -c commit.gpgsign=false commit -q -m change
  git rev-parse HEAD
}
base=$(commit README.md sub/keep.txt)
docs_head=$(commit docs/guide.md)

# [HEAD_SHA=SHA] [STATUS=LINES] run CASE EVENT HEAD [EXPECTED_ALL]
# [EXPECTED_COUNT]: check out HEAD, run the script from sub/ with HEAD_SHA,
# HEAD unless set, and check the outputs, the final checkout and that
# `git status` lists STATUS, nothing unless set.
run() {
  local name=$1 expected_all=${4:-true} expected_count=${5:-all} sha=${HEAD_SHA-$3} now changes
  local want=${sha:-$3}
  git checkout -q "$3"
  : >"$GITHUB_OUTPUT"
  : >"$GITHUB_STEP_SUMMARY"
  : >"$ENV_LOG"
  rm -f "$CHILD_MARK"
  if ! (cd sub && EVENT=$2 HEAD_SHA=$sha bash "$script") >"$scratch/stdout" 2>&1; then
    cat "$scratch/stdout" >&2
    fail "$name: exited nonzero"
  fi
  all=$(output all)
  count=$(output count)
  reason=$(output reason)
  targets=$(output targets)
  file=$(output file)
  alls=$(grep -c '^all=' "$GITHUB_OUTPUT")
  [[ $all == "$expected_all" ]] || fail "$name: want all=$expected_all"
  [[ $count == "$expected_count" ]] || fail "$name: want count=$expected_count"
  [[ $alls == 1 ]] || fail "$name: more than one all output"
  [[ $reason == ?* ]] || fail "$name: no reason"
  grep -qF '### Affected Bazel targets' "$GITHUB_STEP_SUMMARY" || fail "$name: no summary"
  [[ ! -s $ENV_LOG ]] || fail "$name: target-determinator saw runner command files"
  stopped "$scratch/stdout" || fail "$name: a log line can issue a workflow command"
  if [[ $FAKE == fail || $FAKE == hang ]]; then
    sleep 3
    [[ ! -e $CHILD_MARK ]] || fail "$name: a child of target-determinator outlived the run"
  fi
  now=$(git rev-parse HEAD)
  [[ $now == "$want" ]] || fail "$name: the checkout is not $want"
  changes=$("$git_bin" status --porcelain --untracked-files=all --ignore-submodules=none)
  [[ $changes == "${STATUS:-}" ]] || fail "$name: the tree is not as it was"
  if [[ $expected_all == true ]]; then
    [[ $targets == "$fallback" ]] || fail "$name: fallback patterns"
    diff -u <(printf '%s\n' "$fallback") "$file" || fail "$name: fallback pattern file"
    grep -qF 'Running every target' "$GITHUB_STEP_SUMMARY" || fail "$name: summary hides the fallback"
  fi
  echo "PASS: $name: $reason"
}

# VAR=VALUE... refuse NAME [DIR]: the script must fail on a pull request in
# DIR, sub/ by default, and print no workflow command on the way.
refuse() {
  ! (cd "${2:-sub}" && EVENT=pull_request bash "$script") >"$scratch/stdout" 2>&1 || fail "$1: exited zero"
  stopped "$scratch/stdout" || fail "$1: a log line can issue a workflow command"
  echo "PASS: $1"
}

unset HEAD_SHA
export BASE_SHA=$base FAKE=two TIMEOUT_SECONDS=''

git checkout -q "$docs_head"
printf 'VALUE = 1\n' >defs.bzl
starlark_base=$(commit)
printf 'VALUE  = 1\n' >defs.bzl
starlark_head=$(commit)
git config diff.relative true
BASE_SHA=$starlark_base FAKE=none run 'root .bzl formatting edit' pull_request "$starlark_head" false 2
[[ $targets == $'//:buildifier_test\n//:gazelle_test' ]] || fail 'root .bzl edit: missing workspace checks'
git config --unset diff.relative
BASE_SHA=$starlark_base FAKE_QUERY=empty run 'empty candidate query' pull_request "$starlark_head" false 2
[[ $targets == $'//:buildifier_test\n//:gazelle_test' ]] || fail 'empty candidate query: missing workspace checks'
BASE_SHA=$starlark_base FAKE_WORKSPACE=empty run 'workspace checks not found' pull_request "$starlark_head"
[[ $reason == 'the query of workspace checks failed with exit status 1' ]] || fail 'workspace checks not found: wrong reason'

for event in push schedule ''; do
  run "event '$event'" "$event" "$docs_head"
  [[ $reason == "a '$event' event tests everything" ]] || fail 'unsupported event: wrong reason'
done

# A merge head distinguishes an explicit older base from either parent.
git checkout -q "$base"
dispatch_side=$(commit src/cmd.go)
git checkout -q "$docs_head"
dispatch_parent=$(commit src/lib.go)
git -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false \
  merge -q --no-ff --no-edit "$dispatch_side"
dispatch_head=$(git rev-parse HEAD)
BASE_SHA=$base FAKE=dispatch run 'dispatch with explicit base' workflow_dispatch "$dispatch_head" false 3
[[ $targets == $'//:gazelle_test\n//go:cmd_test\n//go:lib_test' ]] || fail 'dispatch explicit base: wrong targets'
BASE_SHA='' FAKE=dispatch run 'dispatch with default base' workflow_dispatch "$dispatch_head" false 2
[[ $targets == $'//:gazelle_test\n//go:cmd_test' ]] || fail 'dispatch default base: wrong targets'
last=$(tail -n 1 "$ARGS_LOG")
[[ $last == "$dispatch_parent" ]] || fail 'dispatch default base: expected the first parent SHA'
BASE_SHA=0123456789abcdef0123456789abcdef01234567 run 'dispatch with missing base' workflow_dispatch "$dispatch_head"
[[ $reason == *'missing from the checkout'* ]] || fail 'dispatch missing base: wrong reason'
BASE_SHA='' run 'dispatch of root commit' workflow_dispatch "$base"
[[ $reason == *"'HEAD^' is missing from the checkout"* ]] || fail 'dispatch of root commit: wrong reason'

run 'pull request' pull_request "$docs_head" false 3
[[ $targets == $'//:gazelle_test\n//go:cmd_test\n//go:lib_test' ]] || fail 'targets output'
grep -qF 'Selected 3 test targets' "$GITHUB_STEP_SUMMARY" || fail 'summary count'
for arg in -bazel bazelisk -targets -bazel-opts=--config=ci -bazel-opts=--keep_going \
  -bazel-opts=--skip_incompatible_explicit_targets "$base"; do
  grep -qxF -- "$arg" "$ARGS_LOG" || fail "target-determinator missed argument $arg"
done
last=$(tail -n 1 "$ARGS_LOG")
[[ $last == "$base" ]] || fail 'the base commit is not the last argument'
for arg in cquery --output=jsonproto --config=ci --keep_going --skip_incompatible_explicit_targets; do
  grep -qxF -- "$arg" "$CQUERY_LOG" || fail "cquery missed argument $arg"
done
grep -Fq -- 'set("//:gazelle_test" "//go:cmd_test" "//go:lib_test")' "$CQUERY_LOG" || fail 'cquery missed selected tests'
while IFS= read -r pattern; do
  grep -Fq -- "${pattern#-}" "$QUERY_LOG" || fail "candidate query missed $pattern"
done <<<"$fallback"
grep -Fq -- 'except set(@tbhb_rules//tools:mutmut_test @tbhb_rules_go//tests/self:gomutants_test @tbhb_rules_python//private:mutmut_test)' \
  "$QUERY_LOG" || fail 'candidate query did not exclude mutation tests before hashing'
FAKE_QUERY=fail run 'candidate query failure' pull_request "$docs_head"
[[ $reason == *'candidate tests failed with exit status 6' ]] || fail 'candidate query failure: status missing'

# `bazel test //...` can still run a manual test through a test_suite.
FAKE=tagged run 'manual tag' pull_request "$docs_head"
[[ $reason == *'tagged manual'* ]] || fail 'manual tag: wrong reason'
for tag in 'review manual' 'review, manual'; do
  FAKE_TAG=$tag FAKE_QUERY=near_manual FAKE=near_manual run "near-match manual tag '$tag'" pull_request "$docs_head" false 2
  [[ $targets == $'//:gazelle_test\n//go:near_manual_test' ]] || fail 'near-match manual tag: missing test'
done
FAKE_CQUERY=fail run 'tag cquery failure' pull_request "$docs_head"
[[ $reason == *'exit status 7' ]] || fail 'tag cquery failure: status missing'

cli_head=$(commit factory/cli/src/tbhbdev/alpha/repo_token.py)
BASE_SHA=$docs_head FAKE=cli_leaf run 'CLI leaf edit' pull_request "$cli_head" false 11
cli_tests=$(printf '%s\n' "$targets" | grep '^//factory/cli/tests:')
[[ $cli_tests == $'//factory/cli/tests:test_alpha_agent_invariants\n//factory/cli/tests:test_alpha_repo_token' ]] ||
  fail 'CLI leaf edit: wrong per-file tests'
[[ $targets != *mutmut_test* ]] || fail 'CLI leaf edit: selected nightly mutation test'
grep -qF 'Selected 11 test targets' "$GITHUB_STEP_SUMMARY" || fail 'CLI leaf edit: summary count'

FAKE=space run 'label with a space' pull_request "$docs_head" false 3
[[ $targets == $'//:gazelle_test\n//a:with space\n//b:t' ]] || fail 'space: targets output'
grep -qxF '//a:with space' "$file" || fail 'space: file'

HEAD_SHA=$docs_head run 'merge group' merge_group "$docs_head" false 3
HEAD_SHA=$docs_head run 'checkout behind the head' merge_group "$base"
FAKE=none run 'only a workspace check' pull_request "$docs_head" false 1
[[ $targets == '//:gazelle_test' ]] || fail 'only a workspace check: targets output'
FAKE=none run 'no changed files' pull_request "$base" false 0
[[ -z $targets ]] || fail 'empty selection'
FAKE=smudge run 'changed under a selection' pull_request "$docs_head"
FAKE=fail run 'tool failure' pull_request "$docs_head"
[[ $reason == *'exit status 3' ]] || fail 'failure status missing'
first=$file
run 'second run in the job' pull_request "$docs_head" false 3
[[ $file != "$first" ]] || fail 'second run: same file'
diff -u <(printf '%s\n' "$fallback") "$first" || fail 'second run: replaced the first file'
FAKE=bare run 'log with no last newline' pull_request "$docs_head"
BAZEL_OPTS=$'--config=ci\n--keep_going' refuse 'bazel-opts on two lines'
FAKE=hang TIMEOUT_SECONDS=1 run 'timeout' pull_request "$docs_head"
[[ $reason == *'timed out'* ]] || fail 'timeout reason missing'
TD='' run 'no tool' pull_request "$docs_head"
TIMEOUT_MINUTES=1.5 run 'bad timeout-minutes' pull_request "$docs_head"
BASE_SHA='' run 'no base' pull_request "$docs_head"
BASE_SHA=0123456789abcdef0123456789abcdef01234567 run 'base not fetched' pull_request "$docs_head"
BREAK=diff PATH="$scratch/broken:$PATH" run 'git diff failure' pull_request "$docs_head"
[[ $reason == 'git diff failed' ]] || fail 'git diff: wrong reason'
BREAK=status PATH="$scratch/broken:$PATH" run 'git status failure' pull_request "$docs_head"
QUERY=$'tests(//...)\n+ //x' run 'query on two lines' pull_request "$docs_head"
HEAD_SHA='' run 'no event head' pull_request "$docs_head"

# target-determinator would analyze a change on top of the head, even one
# that status.showUntrackedFiles=no hides, so the script leaves it alone.
echo dirty >>README.md
STATUS=' M README.md' run 'changed file' pull_request "$docs_head"
git checkout -q README.md
git config status.showUntrackedFiles no
: >sub/stray
STATUS='?? sub/stray' run 'hidden untracked file' pull_request "$docs_head"
rm sub/stray
# An index flag hides a changed file from git status, so the script tests everything.
for flag in assume-unchanged skip-worktree; do
  git update-index "--$flag" README.md
  echo dirty >>README.md
  run "$flag" pull_request "$docs_head"
  git update-index "--no-$flag" README.md
  git checkout -q README.md
done

# Rules-only BUILD and source changes use the configured graph, across repos.
git checkout -q "$base"
rules_head=$(commit lib/rules/diagrams/d2/private/BUILD.bazel lib/rules/diagrams/d2/private/inputs.txt)
for event in pull_request merge_group; do
  FAKE=rules run "rules dependents on $event" "$event" "$rules_head" false 6
  [[ $targets == $'//:gazelle_test\n//diagrams:integration_test\n@@tbhb_rules+//:buildifier_test\n@@tbhb_rules+//repo/tests:markdown_links_test\n@@tbhb_rules_d2+//private:inputs_test\n@@tbhb_rules_structurizr+//private:d2_test' ]] ||
    fail 'rules dependents: targets output'
  diff -u <(printf '%s\n' "$targets") "$file" || fail 'rules dependents: pattern file'
  grep -Fq -- '@tbhb_rules_d2//private:inputs_test' "$ARGS_LOG" || fail 'rules candidates were not hashed'
done
FAKE=rules_tagged run 'manual rules test' merge_group "$rules_head"
[[ $reason == *'tagged manual'* ]] || fail 'manual rules test: wrong reason'
TD='' run 'no tool for a rules change' merge_group "$rules_head"

# The test's own rule stays unchanged when a suite in another package enables it.
git checkout -q "$base"
mkdir -p lib/rules/diagrams/d2/private
cat >lib/rules/diagrams/d2/private/BUILD.bazel <<'BUILD'
sh_test(name = "manual_test", srcs = ["manual.sh"], tags = ["manual"])
BUILD
echo 'test_suite(name = "suite", tests = [])' >lib/rules/diagrams/d2/BUILD.bazel
suite_base=$(commit lib/rules/diagrams/d2/private/manual.sh)
echo 'test_suite(name = "suite", tests = ["//private:manual_test"])' >lib/rules/diagrams/d2/BUILD.bazel
suite_head=$(commit)
BASE_SHA=$suite_base FAKE_QUERY=suite FAKE=none run 'suite enables an unchanged manual test' pull_request "$suite_head"
[[ $reason == 'the candidate query found tests newly runnable at the head' ]] || fail 'suite membership: wrong reason'
cat >lib/rules/diagrams/d2/BUILD.bazel <<'BUILD'
filegroup(name = "not_test")
test_suite(name = "suite", tests = ["//private:manual_test", ":not_test"])
BUILD
bad_suite_head=$(commit)
BASE_SHA=$suite_head FAKE_QUERY=suite FAKE=none run 'suite includes a filegroup' pull_request "$bad_suite_head"
[[ $reason == *'candidate tests failed with exit status 6' ]] || fail 'invalid suite member: wrong reason'

git checkout -q "$rules_head"
rules_config=$(commit lib/rules/diagrams/d2/preset.bazelrc)
BASE_SHA=$rules_head run 'rules preset change' pull_request "$rules_config"
[[ $reason == 'lib/rules/diagrams/d2/preset.bazelrc changed' ]] || fail 'rules preset: wrong reason'
git checkout -q "$base"
cli_head=$(commit cli/main.go)
run 'root-only change' pull_request "$cli_head" false 3
[[ $targets == $'//:gazelle_test\n//go:cmd_test\n//go:lib_test' ]] || fail 'root-only change selected rules tests'

git checkout -q "$docs_head"
head=$(commit go/BUILD.bazel src/lib.go)
FAKE=two_and_buildifier run 'root BUILD change' pull_request "$head" false 4
[[ $targets == $'//:buildifier_test\n//:gazelle_test\n//go:cmd_test\n//go:lib_test' ]] ||
  fail 'root BUILD change: wrong tests'

# These checks read the Git checkout, including nested modules outside the
# rules module's dependency graph.
for check in 'repo/tests:mktemp_test|lib/rules/go/tools/install.sh.tpl' \
  'repo/tests:markdown_links_test|lib/rules/go/README.md' \
  'lint/tests:gitleaks_configs_test|lib/rules/go/.gitleaks.toml' \
  'lint/tests:gitleaks_configs_test|LICENSES/MIT.txt'; do
  git checkout -q "$base"
  path=${check#*|}
  label=${check%|*}
  check_base=$(commit "$path")
  check_head=$(commit "$path")
  BASE_SHA=$check_base FAKE=none run "$path content edit" pull_request "$check_head" false 2
  [[ $targets == "//:gazelle_test"$'\n'"@@tbhb_rules+//$label" ]] || fail "$path: missing $label"
done

git checkout -q "$base"
link_target=$(commit lib/rules/go/target.txt)
git rm -q lib/rules/go/target.txt
link_target_deleted=$(commit)
BASE_SHA=$link_target FAKE=none run 'Markdown link target removed' pull_request "$link_target_deleted" false 2
[[ $targets == $'//:gazelle_test\n@@tbhb_rules+//repo/tests:markdown_links_test' ]] || fail 'link target removed: wrong tests'

git checkout -q "$base"
ln -s README.md link
linked=$(commit)
run 'symlink change' pull_request "$linked"
[[ $reason == 'the symlink link changed' ]] || fail 'symlink change: wrong reason'

# No checkout at all: the event's head can't be checked, so the step fails.
refuse 'no checkout' "$scratch/no-git"

# An event head the step can't check out fails it. The checkout fails
# over a local file, and its name prints with commands stopped.
git checkout -q "$base"
notice=$(commit '##[notice]x')
git checkout -q "$base"
echo changed >'##[notice]x'
HEAD_SHA=$notice refuse 'failed checkout'
rm '##[notice]x'

for path in MODULE.bazel MODULE.bazel.lock .bazelrc .bazelrc.user preset.bazelrc \
  WORKSPACE WORKSPACE.bazel WORKSPACE.extra .bazelversion go/MODULE.bazel \
  go/deps.MODULE.bazel 'café/MODULE.bazel' \
  .bazelignore REPO.bazel; do
  git checkout -q "$docs_head"
  head=$(commit "$path" src/lib.go)
  run "$path" pull_request "$head"
  [[ $reason == "$path changed" ]] || fail "$path: wrong reason"
done

# A file name can't issue a workflow command through the log, or add
# outputs or Markdown to the summary.
git checkout -q "$docs_head"
head=$(commit $'x ::set-output name=all::false ::add-mask::y\nall=false/[a](b)*_/MODULE.bazel')
run 'hostile path' pull_request "$head"
grep -qF -- '::set-output name=all::false' "$scratch/stdout" || fail 'hostile path: name not logged'
grep -qF '\:\:add\-mask\:\:y\?all\=false\/\[a\]\(b\)\*\_\/MODULE\.bazel' "$GITHUB_STEP_SUMMARY" ||
  fail 'hostile path: the summary does not escape the name'

# A rename lists only the new path unless the script turns renames off.
git checkout -q "$docs_head"
module_base=$(commit go/MODULE.bazel)
git mv go/MODULE.bazel go/MODULE.txt
renamed=$(commit)
BASE_SHA=$module_base run 'renamed module' pull_request "$renamed"
[[ $reason == 'go/MODULE.bazel changed' ]] || fail 'rename: wrong reason'

# An empty query input uses the selector's complete candidate scope.
grep -qxF "    default: ''" "$action" || fail 'default query narrows the candidate scope'
echo 'PASS: default query'

# A stopped checkout can leave the base's content under the event's head, in
# the tree and in a submodule. The step restores both, or fails when it can't.
mkdir -p "$scratch/lib" "$scratch/mixed/sub"
cd "$scratch/lib"
git init -q -b main .
lib_base=$(commit lib.txt)
lib_head=$(commit lib.txt)
cd "$scratch/mixed"
git init -q -b main .
git submodule add -q "$scratch/lib" lib
mixed_base=$(commit README.md sub/keep.txt)
mixed_head=$(commit README.md)
BASE_SHA=$mixed_base FAKE=mixed run 'mixed tree' pull_request "$mixed_head"
now=$(git -C lib rev-parse HEAD)
[[ $now == "$lib_head" ]] || fail 'mixed tree: the submodule is not at the head'
BASE_SHA=$mixed_base HEAD_SHA=$mixed_head FAKE=litter refuse 'untracked file left behind'
rm sub/stray

# A diff of only a submodule's commit lists no file target-determinator
# can trace, so the script tests everything.
git -C lib checkout -q "$lib_base"
bumped=$(commit)
BASE_SHA=$mixed_head run 'submodule commit' pull_request "$bumped"
[[ $reason == 'the submodule lib changed' ]] || fail 'submodule commit: wrong reason'

# `ignore = all` in .gitmodules hides a submodule's commit from git diff.
git config -f .gitmodules submodule.lib.ignore all
ignored=$(commit)
git -C lib checkout -q "$lib_head"
git update-index --cacheinfo "160000,$lib_head,lib"
hidden=$(commit)
BASE_SHA=$ignored run 'ignored submodule commit' pull_request "$hidden"
