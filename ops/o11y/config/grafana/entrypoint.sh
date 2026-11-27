#!/bin/sh
# A published loopback connection presents the bridge gateway as its peer.
# Discover one address on every start; never trust the whole container subnet.
set -eu

gateway=$(awk '$2 == "00000000" && $3 != "00000000" && $4 ~ /[37BFbf]$/ { print $3; exit }' /proc/net/route)
case $gateway in
????????) ;;
*)
  echo 'grafana: no default gateway; refusing proxy authentication' >&2
  exit 1
  ;;
esac
# /proc/net/route stores the IPv4 address as little-endian hex.
gateway=$((0x$gateway))
GF_AUTH_PROXY_WHITELIST=$(printf '%d.%d.%d.%d' \
  "$((gateway & 255))" "$(((gateway >> 8) & 255))" \
  "$(((gateway >> 16) & 255))" "$(((gateway >> 24) & 255))")
export GF_AUTH_PROXY_WHITELIST
exec /run.sh "$@"
