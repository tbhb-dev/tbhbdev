#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

version=v0.34.1
system=$(uname -sm)
case "$system" in
Linux\ x86_64)
  arch=amd64
  sha=7f3360cf38d43d4d106440e8ce9d394d4b16bde52af11cd010e897394cec439b
  ;;
Linux\ aarch64)
  arch=arm64
  sha=55d362d87b1c8d946b50ccc483d32ef48620ad1dd95eebd14e2ad41baf1baa2c
  ;;
*) exit 1 ;;
esac
bin=$1
mkdir -p "${bin%/*}"
curl -fsSL --retry 3 -o "$bin" \
  "https://github.com/bazel-contrib/target-determinator/releases/download/${version}/target-determinator.linux.${arch}"
echo "${sha}  ${bin}" | sha256sum -c --quiet -
chmod +x "$bin"
