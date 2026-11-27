#!/usr/bin/env bash
# Controller fixture: accepts an op reference, never contacts 1Password.
set -euo pipefail
[[ $1 == "op://"* ]]
printf '%s' molecule-fixture
