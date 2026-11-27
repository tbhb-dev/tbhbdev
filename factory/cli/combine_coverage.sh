#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

coverage="$PWD/$1"
export RUNFILES_DIR="${RUNFILES_DIR:-${PWD%/*}}"
cd "${BUILD_WORKSPACE_DIRECTORY:?Run this target with bazel run}"
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT

bazelisk query 'kind("^py_test rule$", //factory/cli/tests/...)' >"$temp/targets"
test -s "$temp/targets"

# The checked-in coverage config follows Python subprocesses. Keep raw
# branch arcs from each test until the single coverage.py report.
test_status=0
# Download the raw coverage files so they can be read from local paths.
bazelisk test --config=ci --remote_download_regex='.*/test\.outputs/\.coverage$' \
  --target_pattern_file="$temp/targets" \
  --build_event_json_file="$temp/events.json" \
  --nozip_undeclared_test_outputs \
  --nocache_test_results \
  --test_arg=--cov=tbhbdev --test_arg=--cov-config=pyproject.toml \
  --test_arg=--cov-branch --test_arg=--cov-report= --test_arg=--cov-fail-under=0 \
  || test_status=$?

echo 'Ten slowest targets (milliseconds)'
jq -rs '[.[] | select(.testSummary.firstStartTimeMillis) |
  [((.testSummary.lastStopTimeMillis | tonumber) -
    (.testSummary.firstStartTimeMillis | tonumber)), .id.testSummary.label]] |
  sort_by(.[0]) | reverse | .[:10][] | @tsv' "$temp/events.json"
((test_status == 0)) || exit "$test_status"

logs=$(bazelisk info --config=ci bazel-testlogs)
# Read the artifacts from this invocation. Old shard directories can still
# exist in bazel-testlogs after a shard count changes.
jq -r '
  select(.testResult) |
  [.testResult.testActionOutput[] |
    select(.name == "test.outputs/.coverage" and (.uri | startswith("file://")))] |
  if length == 1 then .[0].uri | ltrimstr("file://")
  else error("Expected one local branch coverage file per test action")
  end
' "$temp/events.json" >"$temp/reports"
test -s "$temp/reports"
i=0
while IFS= read -r report; do
  ((i += 1))
  cp "$report" "$temp/.coverage.$i"
done <"$temp/reports"

cd factory/cli
export COVERAGE_FILE="$temp/.coverage"
"$coverage" combine "$temp"
"$coverage" json --fail-under=0 --include='src/tbhbdev/*' -o "$temp/report.json"
find src/tbhbdev -type f -name '*.py' | LC_ALL=C sort >"$temp/source"
jq -r '.files | keys[]' "$temp/report.json" | LC_ALL=C sort >"$temp/measured"
diff -u "$temp/source" "$temp/measured"
"$coverage" lcov --fail-under=0 --include='src/tbhbdev/*' -o "$logs/factory/cli/coverage.lcov"
"$coverage" report --fail-under=100 --include='src/tbhbdev/*'
