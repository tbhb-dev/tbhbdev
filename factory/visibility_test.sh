#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

lib_query="$1"
factory_query="$2"

grep -qF '<string value="//products/..."/>' "$lib_query"
grep -qF '<string value="//lib/..."/>' "$lib_query"
grep -qF '<string value="//factory/..."/>' "$lib_query"
grep -qF '<string value="//factory/..."/>' "$factory_query"
if grep -E "//products|public" "$factory_query" >/dev/null; then exit 1; fi
if grep -E '<rule-input name="//(factory|products)[:/]' "$lib_query" >/dev/null; then exit 1; fi

grep -qF '"//factory:__subpackages__"' lib/BUILD.bazel
grep -qF '"//lib:__subpackages__"' lib/BUILD.bazel
grep -qF '"//products:__subpackages__"' lib/BUILD.bazel

shopt -s nullglob
for bf in factory/BUILD.bazel factory/*/BUILD.bazel; do
  grep -qF 'package(default_visibility = ["//factory:__subpackages__"])' "$bf"
  if grep -F "//products" "$bf" >/dev/null; then exit 1; fi
done

grep -qF "stdlib-only-front" .golangci.yml
grep -qF "stdlib-only-front" .go-arch-lint.yml
grep -qF -- "--test_timeout=" .bazelrc
grep -qF 'timeout = "short"' factory/go.bzl
if find factory lib products docs third_party -type d \( -name core -o -name shell \) | grep -q .; then exit 1; fi
