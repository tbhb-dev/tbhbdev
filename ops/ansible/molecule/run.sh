#!/usr/bin/env bash
# run.sh -- run one role's Molecule scenario in Docker, for `mise run molecule`.
#
# The image comes from molecule/image, the target named after the role, and
# its tag carries the Dockerfile's hash, so an edit builds a new one. The
# toolchain is ansible/'s own uv project.
#
# Usage: molecule/run.sh ROLE [molecule test args...], from ansible/.
set -euo pipefail

role=${1:-}
[[ -n $role && -d roles/$role/molecule ]] || {
  echo "usage: mise run molecule -- ROLE [molecule test args...]; ROLE needs roles/ROLE/molecule" >&2
  exit 2
}
shift

uv run --quiet ansible-galaxy collection install -r requirements.yml -p collections >/dev/null
hash=$(sha256sum molecule/image/Dockerfile | cut -c1-12)
image=ops-molecule:$role-$hash
if ! docker image inspect "$image" >/dev/null 2>&1; then
  timeout --kill-after=30 600 docker build --progress=plain --target "$role" --tag "$image" molecule/image || {
    status=$?
    if ((status == 124 || status == 137)); then
      printf 'Molecule image build timed out for role %s (image %s)\n' "$role" "$image" >&2
    fi
    exit "$status"
  }
fi

root=$PWD
cd "roles/$role"
MOLECULE_IMAGE=$image MOLECULE_RUN_ID=$$ \
  ANSIBLE_COLLECTIONS_PATH=$root/collections ANSIBLE_ROLES_PATH=$root/roles \
  exec uv run --project "$root" molecule test "$@"
