#!/bin/sh
set -eu

# Where arm saves the firewall as it stood. A missing table.nft means the
# table was absent, and a missing host.nft means no persisted file existed.
state=/var/lib/host-hardening
persisted=/etc/nftables.d/host.nft
unit=host-hardening-nft.service

cancel_revert() {
  # Either transient unit may be absent on a first or completed run.
  for u in host-hardening-revert.timer host-hardening-revert.service; do
    unit_state=$(systemctl show --property=LoadState --value "$u")
    if [ "$unit_state" != not-found ]; then
      systemctl stop "$u"
      # --collect may unload the unit as soon as it stops.
      unit_state=$(systemctl show --property=LoadState --value "$u")
      if [ "$unit_state" = loaded ]; then
        systemctl reset-failed "$u"
      fi
    fi
  done
}

case ${1:-revert} in
arm)
  for u in host-hardening-revert.timer host-hardening-revert.service; do
    unit_state=$(systemctl show --property=LoadState --value "$u")
    if [ "$unit_state" != not-found ]; then
      echo 'Interrupted hardening run detected; reverting before rearming.' >&2
      "$0" revert
      break
    fi
  done
  cancel_revert
  install -d -m 0700 "$state"
  rm -f "$state/table.nft" "$state/host.nft"
  if nft list table inet host >"$state/table.nft.new" 2>/dev/null; then
    mv "$state/table.nft.new" "$state/table.nft"
  else
    rm -f "$state/table.nft.new"
  fi
  if [ -f "$persisted" ]; then
    cp -p "$persisted" "$state/host.nft"
  fi
  systemd-run --unit=host-hardening-revert --on-active=3min --collect \
    /usr/local/sbin/host-hardening-revert
  ;;
cancel)
  cancel_revert
  ;;
revert)
  # A failed recovery step must not prevent the remaining recovery steps.
  status=0
  # Only table inet host: Docker's and libvirt's tables stay as they are.
  nft delete table inet host 2>/dev/null || true
  if [ -f "$state/table.nft" ]; then
    nft -f "$state/table.nft" || status=1
  fi
  if [ -f "$state/host.nft" ]; then
    cp -p "$state/host.nft" "$persisted" || status=1
  else
    rm -f "$persisted" || status=1
    systemctl disable "$unit" 2>/dev/null || true
  fi
  unit_state=$(systemctl show --property=LoadState --value host-hardening-revert.timer) || status=1
  if [ "$unit_state" != not-found ]; then
    systemctl stop host-hardening-revert.timer || status=1
  fi
  exit "$status"
  ;;
*)
  echo 'usage: host-hardening-revert [arm|cancel|revert]' >&2
  exit 2
  ;;
esac
