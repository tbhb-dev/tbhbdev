#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:ignore EXECROOT

set -euo pipefail
self=@@SELF@@
if [[ -d "$0.runfiles/_main" ]]; then
  base="$0.runfiles/_main"
elif [[ -n ${RUNFILES_DIR:-} ]]; then
  base="$RUNFILES_DIR/_main"
else
  base=${0%"/$self"}
fi
RUNFILES_DIR=$(cd "$base/.." && pwd)
export RUNFILES_DIR
root=${BUILD_WORKSPACE_DIRECTORY:?run with bazel run}
repo_root=@@REPO_ROOT@@
if [[ -n $repo_root ]]; then
  root="${BUILD_EXECROOT:?run with bazel run}/$repo_root"
fi
exec "$base/@@TOOL@@" "$root" \
  --manifest "$base/@@MANIFEST@@" @@ARGS@@ "$@"
