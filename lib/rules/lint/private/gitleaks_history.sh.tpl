#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# Scans the workspace's git history with gitleaks. Generated from
# gitleaks_history.sh.tpl; the @@NAME@@ markers are filled in at analysis
# time. Run with `bazel run`. A shallow clone scans only the commits it has.
# Arguments go to `gitleaks git` before the path, so `--log-opts=HEAD` limits
# the scan to the checked-out branch; without it gitleaks scans every ref.

set -euo pipefail

runfiles=$PWD
tool="$runfiles/@@TOOL@@"
git_dir="$runfiles/@@GIT_DIR@@"
config=@@CONFIG@@

export PATH="$git_dir:$PATH"
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null

cd "${BUILD_WORKSPACE_DIRECTORY:?run this target with bazel run}"

if ! git rev-parse --verify --quiet HEAD >/dev/null; then
  echo "gitleaks_history: FAIL: the repository has no commits to scan" >&2
  exit 1
fi

args=(git --verbose --redact --no-banner)
if [[ -n $config ]]; then
  args+=(--config "$config")
fi

log=$(mktemp "${TMPDIR:-/tmp}/gitleaks-history.XXXXXX")
history=$(mktemp "${TMPDIR:-/tmp}/gitleaks-history.XXXXXX")
trap 'rm -f "$log" "$history"' EXIT
# To a file, not a pipe, so gitleaks' own exit status survives.
status=0
"$tool" "${args[@]}" "$@" . >"$log" 2>&1 || status=$?
cat "$log"
if ((status != 0)); then
  exit "$status"
fi

# gitleaks exits 0 when its git invocation fails, after scanning nothing.
# Only additions need a scan. Numstat avoids patch headers and deletions;
# rename detection keeps unchanged renames at zero additions. Binary rows
# have unknown added counts (-), so conservatively require a scan.
if ! grep -Eq '[1-9][0-9]* commits scanned' "$log"; then
  if ! git log --format= --numstat --find-renames HEAD >"$history"; then
    echo "gitleaks_history: FAIL: git cannot read the history that gitleaks scanned 0 commits of" >&2
    exit 1
  fi
  if awk '$1 == "-" || $1 + 0 > 0 { found = 1 } END { exit !found }' "$history"; then
    echo "gitleaks_history: FAIL: gitleaks scanned 0 commits of a history with added content, which proves nothing" >&2
    exit 1
  fi
  echo "gitleaks_history: the history holds no added content to scan"
fi
