#!/bin/sh
# Fails when any OpenTofu file under tofu/ is not in canonical format.
# Arguments are runfiles paths for the binary and a module marker.
set -eu

runfiles="${RUNFILES_DIR:-$0.runfiles}"
tofu="$runfiles/$1"
root="$(dirname "$(dirname "$runfiles/$2")")"

exec "$tofu" fmt -check -diff -recursive "$root"
