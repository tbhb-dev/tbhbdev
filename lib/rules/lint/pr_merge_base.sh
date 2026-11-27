#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

if [[ ${1:-} == --ref ]]; then
  shift
  local_ref=1
else
  local_ref=0
fi
if (($# != 2)); then
  echo 'usage: pr_merge_base BASE_REF HEAD (or pr_merge_base --ref BASE HEAD)' >&2
  exit 2
fi

cd "${BUILD_WORKSPACE_DIRECTORY:?run this target with bazel run}"
base=$1
if ((local_ref == 0)); then
  base="refs/remotes/origin/$1"
  timeout -k 5s 60s git fetch origin "+refs/heads/$1:$base" >&2 || {
    status=$?
    printf 'pr_merge_base: could not fetch branch %s from origin (exit status %s)\n' "$1" "$status" >&2
    exit "$status"
  }
fi
exec git merge-base -- "$base" "$2"
