# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go build and check hooks with a per-package test time cap."""

load("@rules_go//go:def.bzl", _go_binary = "go_binary", _go_library = "go_library", _go_test = "go_test")
load(
    "@tbhb_rules_go//:defs.bzl",
    _go_arch_lint_test = "go_arch_lint_test",
    _go_deadcode_test = "go_deadcode_test",
    _go_mod_tidy_test = "go_mod_tidy_test",
    _gobco_test = "gobco_test",
    _golangci_lint_aspect_factory = "golangci_lint_aspect_factory",
    _golangci_lint_test_factory = "golangci_lint_test_factory",
    _gomutants_test = "gomutants_test",
    _govulncheck = "govulncheck",
    _govulncheck_test = "govulncheck_test",
)

go_binary = _go_binary
go_library = _go_library
go_arch_lint_test = _go_arch_lint_test
go_deadcode_test = _go_deadcode_test
go_mod_tidy_test = _go_mod_tidy_test
gobco_test = _gobco_test
gomutants_test = _gomutants_test
govulncheck = _govulncheck
govulncheck_test = _govulncheck_test

golangci_lint = _golangci_lint_aspect_factory(
    config = "//:.golangci.yml",
    go_mod = Label("//:go.mod"),
)
golangci_lint_test = _golangci_lint_test_factory(golangci_lint)

def go_test(name, size = "small", timeout = "short", **kwargs):
    """Wraps rules_go's go_test with a per-package small/short (60s) time cap."""
    _go_test(name = name, size = size, timeout = timeout, **kwargs)
