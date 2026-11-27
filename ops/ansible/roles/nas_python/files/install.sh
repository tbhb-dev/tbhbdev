#!/bin/sh
# Install a pinned uv, then a uv-managed CPython, into the connecting user's
# home. No root is needed, and nothing outside the home changes. POSIX sh and
# curl only, because DSM's Python 3.8 is below ansible-core's floor of 3.9.
#
# Usage: install.sh MODE UV_VERSION PYTHON_VERSION SHA256_X86_64 SHA256_AARCH64
#   MODE apply installs what is missing; MODE check changes nothing.
# It prints "nas_python: interpreter=PATH" and then "nas_python: changed" or
# "nas_python: unchanged". In check mode, changed means apply would change it.
set -eu

mode=$1 uv_version=$2 python_version=$3 sha_x86_64=$4 sha_aarch64=$5
bin=$HOME/.local/bin
link=$bin/python$python_version

for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "nas_python: $tool is missing" >&2
    exit 1
  }
done

case $(uname -m) in
x86_64) arch=x86_64 sha=$sha_x86_64 ;;
aarch64) arch=aarch64 sha=$sha_aarch64 ;;
*)
  machine=$(uname -m)
  echo "nas_python: no uv build for $machine" >&2
  exit 1
  ;;
esac

# The target of the Python link, or none when it is missing or broken.
python_state() {
  if [ -x "$link" ]; then readlink "$link"; else echo none; fi
}

changed=unchanged

case $("$bin/uv" --version 2>/dev/null || true) in
"uv $uv_version" | "uv $uv_version "*) ;;
*)
  changed=changed
  if [ "$mode" = apply ]; then
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    name=uv-$arch-unknown-linux-gnu
    curl --fail --silent --show-error --location --output "$work/uv.tar.gz" \
      "https://github.com/astral-sh/uv/releases/download/$uv_version/$name.tar.gz"
    echo "$sha  $work/uv.tar.gz" | sha256sum -c - >/dev/null
    tar -xzf "$work/uv.tar.gz" -C "$work"
    mkdir -p "$bin"
    cp "$work/$name/uv" "$bin/.uv.new"
    chmod 0755 "$bin/.uv.new"
    mv -f "$bin/.uv.new" "$bin/uv"
  fi
  ;;
esac

before=$(python_state)
if [ "$mode" = apply ]; then
  UV_PYTHON_BIN_DIR=$bin UV_PYTHON_INSTALL_BIN=1 \
    "$bin/uv" python install "$python_version" >/dev/null
  "$link" -c 'import sys; print(sys.version)' >/dev/null
  after=$(python_state)
  [ "$after" = "$before" ] || changed=changed
elif [ "$before" = none ]; then
  changed=changed
fi

echo "nas_python: interpreter=$link"
echo "nas_python: $changed"
