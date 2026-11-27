#!/usr/bin/env bash
# Run deployment OpenTofu with values supplied by a companion checkout.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)
companion=${TBHB_PRIVATE_ROOT:-../tbhbdev-private}
[[ $companion = /* ]] || companion="$root/$companion"

if [[ ${1:-} != -chdir=* ]]; then
  printf '%s\n' 'Use -chdir=ops/tofu/<module> and specify an OpenTofu command.' >&2
  exit 2
fi
module_dir=$(cd "${1#-chdir=}" && pwd)
shift
if [[ $(dirname "$module_dir") != "$here" ]]; then
  printf '%s\n' 'Choose an OpenTofu module directly under ops/tofu.' >&2
  exit 2
fi
settings="$companion/ops/tofu/${module_dir##*/}"
env_file=${TBHB_TOFU_OP_ENV:-"$companion/ops/tofu/op.env"}
args=()
if [[ ${1:-} == init ]] && grep -q 'backend "s3"' "$module_dir/versions.tf"; then
  args+=("-backend-config=${TBHB_TOFU_BACKEND_CONFIG:-"$settings/backend.tfbackend"}")
elif [[ ${1:-} == plan || ${1:-} == apply ]]; then
  for values in "$settings"/*.tfvars "$settings"/*.tfvars.json; do
    [[ ! -f $values ]] || args+=("-var-file=$values")
  done
fi

# The 1Password provider rejects an account alongside a service token.
if [[ -n ${OP_SERVICE_ACCOUNT_TOKEN:-} ]]; then
  unset OP_ACCOUNT
fi
command=${1:?Specify an OpenTofu command}
shift
exec op run --env-file "$env_file" -- "$root/.bazel_env/bin/tofu"   "-chdir=$module_dir" "$command" "${args[@]}" "$@"
