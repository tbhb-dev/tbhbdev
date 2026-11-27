#!/usr/bin/env bash
# Only stdin holds the token. Native stores are read before an update, so
# repeated installs do not modify the keychain.
set -euo pipefail
umask 077
storage=$1
system=$2
file=$3
value=$(cat)
[[ -n $value && $value != *$'\n'* && $value =~ ^[A-Za-z0-9._-]+$ ]]
if [[ $storage == file ]]; then
  dir=${file%/*}
  mkdir -p "$dir"
  fixed=
  dir_mode=$(stat -c %a "$dir" 2>/dev/null || stat -f %Lp "$dir")
  [[ $dir_mode == 700 ]] || fixed=1
  chmod 700 "$dir"
  if [[ -f $file && ! -L $file && -O $file ]]; then
    held=$(cat "$file")
    file_mode=$(stat -c %a "$file" 2>/dev/null || stat -f %Lp "$file")
    if [[ $held == "$value" && $file_mode == 600 && -z $fixed ]]; then
      echo ok
      exit
    fi
  fi
  tmp=$(mktemp "$dir/.connect-token.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  printf '%s' "$value" >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
elif [[ $system == Darwin ]]; then
  mkdir -p "$HOME/Library/Keychains"
  if [[ ! -e $HOME/Library/Keychains/ops-connect.keychain-db ]]; then
    security create-keychain -p '' ops-connect.keychain >/dev/null 2>&1
  fi
  security unlock-keychain -p '' ops-connect.keychain >/dev/null 2>&1
  security set-keychain-settings ops-connect.keychain >/dev/null 2>&1
  held=$(security find-generic-password -s ops-connect -a agent -w ops-connect.keychain 2>/dev/null || true)
  if [[ $held == "$value" ]]; then
    echo ok
    exit
  fi
  # security's interactive parser gets the value through its input stream.
  printf 'add-generic-password -U -s ops-connect -a agent -T /usr/bin/security -w %s ops-connect.keychain\n' "$value" |
    security -i >/dev/null 2>&1
  held=$(security find-generic-password -s ops-connect -a agent -w ops-connect.keychain 2>/dev/null)
  [[ $held == "$value" ]]
else
  echo "native storage is only available on macOS; use file" >&2
  exit 1
fi
echo changed
