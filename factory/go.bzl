# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Factory Go lint and per-package test defaults."""

load("@rules_go//go:def.bzl", _go_binary = "go_binary", _go_library = "go_library", _go_test = "go_test")
load("@tbhb_rules_go//:defs.bzl", "golangci_lint_aspect_factory", "golangci_lint_test_factory")

go_binary = _go_binary
go_library = _go_library

golangci_lint = golangci_lint_aspect_factory(
    config = "//:.golangci.yml",
    go_mod = Label("//:go.mod"),
)

golangci_lint_test = golangci_lint_test_factory(golangci_lint)

def go_test(name, size = "small", timeout = "short", **kwargs):
    """Wraps rules_go's go_test with a per-package small/short (60s) time cap."""
    _go_test(name = name, size = size, timeout = timeout, **kwargs)
