# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Go build macros with a per-package test time cap."""

load("@rules_go//go:def.bzl", _go_binary = "go_binary", _go_library = "go_library", _go_test = "go_test")

go_binary = _go_binary
go_library = _go_library

def go_test(name, size = "small", timeout = "short", **kwargs):
    """Runs rules_go tests with race detection and a default 60s time cap."""
    _go_test(name = name, size = size, timeout = timeout, race = "on", **kwargs)
