#!/usr/bin/env bash
set -euo pipefail
if uv --version >/dev/null 2>&1; then
  echo ok
  exit
fi
tool="uv@$1"
mise install "$tool" >&2
binary=$(mise which uv --tool "$tool")
mkdir -p "$HOME/.local/bin"
ln -sf "$binary" "$HOME/.local/bin/uv"
echo changed
