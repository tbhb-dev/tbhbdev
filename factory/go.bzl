# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go build macros with a per-package test time cap."""

load("@rules_go//go:def.bzl", _go_binary = "go_binary", _go_library = "go_library", _go_test = "go_test")

go_binary = _go_binary
go_library = _go_library

def go_test(name, size = "small", timeout = "short", **kwargs):
    """Wraps rules_go's go_test with a per-package small/short (60s) time cap."""
    _go_test(name = name, size = size, timeout = timeout, **kwargs)
