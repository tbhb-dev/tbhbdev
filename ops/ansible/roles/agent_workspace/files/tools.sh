#!/usr/bin/env bash
set -euo pipefail
for config in mise.toml .mise.toml mise/config.toml .mise/config.toml .config/mise.toml .config/mise/config.toml .tool-versions; do
  if [[ -e $config ]]; then
    if mise install --dry-run-code >/dev/null; then
      echo ok
      exit
    fi
    mise install >&2
    echo changed
    exit
  fi
done
echo ok
