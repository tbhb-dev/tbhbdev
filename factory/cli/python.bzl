# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Runtime dependencies for the CLI's Gazelle-generated Python targets."""

load("@rules_python//python:defs.bzl", "py_library")

def cli_py_library(name, deps = [], extra_deps = [], pyi_deps = [], **kwargs):
    """Build a library with runtime dependencies. Pyrefly checks TYPE_CHECKING imports.

    Args:
        name: Bazel target name.
        deps: Imports found by Gazelle.
        extra_deps: Implicit imports required at runtime.
        pyi_deps: Type-only imports. Some point back to runtime dependents.
        **kwargs: Other Gazelle-generated library attributes.
    """
    _ = pyi_deps
    py_library(name = name, deps = deps + extra_deps, **kwargs)
