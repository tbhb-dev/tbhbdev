#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

# Installs the Vale packages that .vale.ini pins into .vale/, for editors and
# for the pull request preflight. Run it with `bazelisk run //:vale_sync`.

set -euo pipefail

vale="$PWD/${1:?missing pinned Vale runfile}"
cd "${BUILD_WORKSPACE_DIRECTORY:?run this with bazelisk run //:vale_sync}"
exec "$vale" sync
