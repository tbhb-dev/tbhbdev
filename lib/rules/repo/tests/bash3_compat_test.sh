#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# The affected-targets scripts run on stock macOS, whose bash is 3.2. Fail on
# any construct that bash 3.2 lacks, naming the file and line.

set -euo pipefail

if (($# == 0)); then
  for rlocation in ${SCRIPT_RLOCATIONPATH_LIST:?}; do
    set -- "$@" "${TEST_SRCDIR:?}/$rlocation"
  done
fi

status=0
for file in "$@"; do
  # Bash 4 and later: associative arrays, name references, mapfile, readarray,
  # case conversion, appending both streams to a file, and coproc.
  if grep -nE \
    -e '(declare|typeset|local|readonly)[[:space:]]+-[A-Za-z]*[AnN]' \
    -e '(^|[^[:alnum:]_])(mapfile|readarray|coproc)([^[:alnum:]_]|$)' \
    -e '\$\{[!#]?[A-Za-z_][A-Za-z0-9_]*(,,?|\^\^?)[^}]*\}' \
    -e '&>>' \
    "$file" | sed "s|^|bash 3.2: ${file##*/}:|" >&2; then
    status=1
  fi
done
if ((status)); then
  echo 'bash3_compat: FAIL: use only constructs that bash 3.2 supports' >&2
fi
exit "$status"
