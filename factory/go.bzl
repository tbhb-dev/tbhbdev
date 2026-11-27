# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go build and check hooks with a per-package test time cap.

Stub for Go static analysis, architecture, branch-coverage, and mutation
gates (golangci-lint, go-arch-lint, gobco, gomutants, govulncheck, deadcode,
and go mod tidy): those macros arrive with the rules import. Until then,
this module wraps rules_go targets and enforces the per-package small/short
(60s) test time cap.
"""

load("@rules_go//go:def.bzl", _go_binary = "go_binary", _go_library = "go_library", _go_test = "go_test")

go_binary = _go_binary
go_library = _go_library

def go_test(name, size = "small", timeout = "short", **kwargs):
    """Wraps rules_go's go_test with a per-package small/short (60s) time cap."""
    _go_test(name = name, size = size, timeout = timeout, **kwargs)
