#!/bin/sh
# Validates each OpenTofu root under tofu/ without the network: init reads
# the providers from a filesystem mirror and nothing else, and each lock file
# must already cover them. Arguments: the tofu binary, then "<provider
# address>=<provider zip>" pairs, then each root's versions.tf, as runfiles
# paths.
set -eu

runfiles="${RUNFILES_DIR:-$0.runfiles}"
tofu="$runfiles/$1"
shift

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

roots=""
for arg in "$@"; do
  case "$arg" in
  *=*)
    dir="$work/mirror/registry.opentofu.org/${arg%%=*}"
    mkdir -p "$dir"
    cp "$runfiles/${arg#*=}" "$dir/"
    ;;
  *)
    roots="$roots $arg"
    ;;
  esac
done

cat >"$work/cli.tfrc" <<TFRC
provider_installation {
  filesystem_mirror {
    path = "$work/mirror"
  }
}
TFRC

export TF_CLI_CONFIG_FILE="$work/cli.tfrc"
export TF_IN_AUTOMATION=1
for versions in $roots; do
  name="$(basename "$(dirname "$versions")")"
  cp -R "$(dirname "$runfiles/$versions")" "$work/$name"
  (
    cd "$work/$name"
    "$tofu" init -backend=false -input=false -lockfile=readonly
    "$tofu" validate
  )
done
