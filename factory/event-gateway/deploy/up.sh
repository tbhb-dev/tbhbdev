#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
#
# Starts the gateway from an image that names its digest. Compose can't check
# the form of a variable's value, so this script refuses any image reference
# that isn't `<name>@sha256:<64 hex digits>` or `sha256:<64 hex digits>`. A
# name is one or more `/`-separated components. A component is runs of
# lowercase letters and digits joined by `.`, `_`, `__` or dashes, so a tag, a
# registry port, a leading dash, an empty component and `.` or `..` are all
# refused. The second form is the image id, for a host that loaded the image.
#
# It also refuses to start unless secrets/github_webhook_secret,
# secrets/event_gateway_session_tokens and secrets/tunnel_token are regular
# files, not symlinks, with mode 0400 and owner uid 65532, because Compose
# leaves file secrets as the host has them, and unless
# EVENT_GATEWAY_SESSION_BIND is a literal IPv4 address in dotted-quad form
# other than 0.0.0.0 (IPv4 only; a hostname, an IPv6 address, a port or a CIDR
# suffix is refused).
#
# Nothing but `--pull never` may follow the image. Without sudo,
# COMPOSE_PROJECT_NAME, COMPOSE_PROFILES, DOCKER_HOST, DOCKER_CONTEXT and
# DOCKER_CONFIG keep their effect, so the caller must control them. An image id
# has no registry to pull from, so it needs `--pull never`.
#
# The host needs grep, dirname and find besides the shell and docker.
#
# Usage: EVENT_GATEWAY_SESSION_BIND=<ipv4> [DEPLOY_DOCKER_SUDO=1] deploy/up.sh <image> [--pull never]
set -eu

# The character classes below mean ASCII only, whatever the caller's locale.
LC_ALL=C
export LC_ALL

image=${1:-}

# case sees the whole string, so a newline can't hide a second line from grep.
case $image in
*[[:space:]]*)
  echo "up.sh: the image must not contain whitespace or a newline" >&2
  exit 2
  ;;
esac

if ! printf '%s\n' "$image" | grep -Eq '^([a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\.|_|__|-+)[a-z0-9]+)*)*@)?sha256:[0-9a-f]{64}$'; then
  echo "up.sh: the image must be <name>@sha256:<64 hex digits> or sha256:<64 hex digits>, got '$image'" >&2
  exit 2
fi

shift

refuse_args() {
  echo "up.sh: the only argument allowed after the image is '--pull never'" >&2
  exit 2
}

case $# in
0) ;;
2) if [ "$1" != --pull ] || [ "$2" != never ]; then refuse_args; fi ;;
*) refuse_args ;;
esac

deploy_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)

bind=${EVENT_GATEWAY_SESSION_BIND:-}
octet='(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'

case $bind in
'' | 0.* | *[[:space:]]*)
  echo "up.sh: EVENT_GATEWAY_SESSION_BIND must be a non-zero IPv4 address" >&2
  exit 2
  ;;
esac

if ! printf '%s\n' "$bind" | grep -Eq "^$octet\\.$octet\\.$octet\\.$octet\$"; then
  echo "up.sh: EVENT_GATEWAY_SESSION_BIND must be a non-zero IPv4 address, got '$bind'" >&2
  exit 2
fi

for name in github_webhook_secret event_gateway_session_tokens tunnel_token; do
  file=$deploy_dir/secrets/$name
  # find prints the path only when the file matches; a missing file prints nothing.
  if [ -L "$file" ] || [ ! -f "$file" ] ||
    [ -z "$(find "$file" -type f -perm 0400 -user 65532 2>/dev/null || true)" ]; then
    echo "up.sh: secrets/$name must be a regular file, not a symlink, with mode 400 and owner uid 65532" >&2
    exit 2
  fi
done

# sudo resets the environment, so env sets the compose variables after it.
if [ "${DEPLOY_DOCKER_SUDO:-0}" = 1 ]; then
  sudo env "EVENT_GATEWAY_IMAGE=$image" "EVENT_GATEWAY_SESSION_BIND=$bind" \
    docker compose -f "$deploy_dir/compose.yaml" up -d --force-recreate "$@"
else
  EVENT_GATEWAY_IMAGE=$image EVENT_GATEWAY_SESSION_BIND=$bind \
    docker compose -f "$deploy_dir/compose.yaml" up -d --force-recreate "$@"
fi
