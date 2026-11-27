#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail
output=$("$PWD/@@COUNTER@@" "$@")
pattern='^\{"code_lines": ([0-9]+), "code_limit": 400, "raw_lines": ([0-9]+), "raw_limit": 1500, "imported_lines": ([0-9]+|null), "failed": ("code"|"raw"|null), "branch_exempt": (true|false)\}$'
[[ $output =~ $pattern ]]
code=${BASH_REMATCH[1]} raw=${BASH_REMATCH[2]} imported=${BASH_REMATCH[3]} failed=${BASH_REMATCH[4]}
branch_exempt=${BASH_REMATCH[5]}
[[ $imported != null ]] || imported=0
echo "pr_size: code=$code/400 raw=$raw/1500 imported=$imported"
if [[ $branch_exempt == true ]]; then
  echo "pr_size: branch $PR_SIZE_HEAD_BRANCH is exempt from the size limit by .github/pr-size-exempt-branches.txt"
fi
case $failed in
null) exit 0 ;;
'"code"') limit='400 added Code lines' ;;
'"raw"') limit='1500 raw added lines' ;;
esac
if [[ $failed == '"code"' ]] && ((raw > 1500)); then
  limit+=' 1500 raw added lines'
fi
echo "pr_size: FAIL: $limit; code=$code/400 raw=$raw/1500; split into stacked PRs" >&2
exit 1
