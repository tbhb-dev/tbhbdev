#!/bin/sh
set -eu

PATH=$PATH:/usr/local/bin:/usr/sbin:/sbin
cd "$(dirname "$0")"

# Read the resolved address of the single RustFS port from Compose.
config=$(docker compose --env-file compose.env config rustfs)
bind_addr=$(printf '%s\n' "$config" | sed -n 's/^[[:space:]]*host_ip: //p')
case "$bind_addr" in
'' | 0.0.0.0 | :: | '*')
  echo 'RustFS boot failed: set S3_BIND_ADDR to a specific host address in compose.env.' >&2
  exit 1
  ;;
esac

failure='S3_BIND_ADDR did not appear on a host interface.'
attempt=0
while [ "$attempt" -lt 60 ]; do
  attempt=$((attempt + 1))
  if ip address show | awk -v bind="$bind_addr" '
    $1 == "inet" || $1 == "inet6" {
      split($2, address, "/")
      if (address[1] == bind) found = 1
    }
    END { exit !found }
  '; then
    if docker compose --env-file compose.env up -d rustfs; then
      exit 0
    fi
    failure='docker compose up failed after S3_BIND_ADDR appeared.'
  fi
  if [ "$attempt" -lt 60 ]; then
    sleep 5
  fi
done

printf 'RustFS boot failed: %s\n' "$failure" >&2
exit 1
