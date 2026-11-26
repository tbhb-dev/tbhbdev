#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

coverage="$PWD/$1"
export RUNFILES_DIR="${RUNFILES_DIR:-${PWD%/*}}"
cd "${BUILD_WORKSPACE_DIRECTORY:?Run this target with bazel run}"
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT

bazelisk query 'kind("^py_test rule$", //factory/cli/tests:all)' >"$temp/targets"
test -s "$temp/targets"

# The checked-in coverage config follows Python subprocesses. Keep raw
# branch arcs from each test until the single coverage.py report.
test_status=0
bazelisk test --config=ci --target_pattern_file="$temp/targets" \
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
while IFS= read -r label; do
  target=${label##*:}
  log_dir="$logs/factory/cli/tests/$target"
  # Each test has an independent data file, including each configured shard.
  mapfile -t reports < <(find "$log_dir" -path '*/test.outputs/.coverage')
  ((${#reports[@]} > 0))
  for i in "${!reports[@]}"; do
    cp "${reports[i]}" "$temp/.coverage.$target.$i"
  done
done <"$temp/targets"

cd factory/cli
export COVERAGE_FILE="$temp/.coverage"
"$coverage" combine "$temp"
"$coverage" json --fail-under=0 --include='src/tbhbdev/*' -o "$temp/report.json"
find src/tbhbdev -type f -name '*.py' | LC_ALL=C sort >"$temp/source"
jq -r '.files | keys[]' "$temp/report.json" | LC_ALL=C sort >"$temp/measured"
diff -u "$temp/source" "$temp/measured"
"$coverage" lcov --fail-under=0 --include='src/tbhbdev/*' -o "$logs/factory/cli/coverage.lcov"
"$coverage" report --fail-under=100 --include='src/tbhbdev/*'
