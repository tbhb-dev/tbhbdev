# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Runtime dependencies for the CLI's Gazelle-generated Python targets."""

load("@rules_python//python:defs.bzl", "py_library")

_SOURCE_ROOT = "factory/cli/src/tbhbdev"

# Importing a submodule runs these initializers. The other __init__.py files
# contain only docstrings and have no runtime dependencies.
_PACKAGE_INITIALIZERS = [
    _SOURCE_ROOT + "/admin",
    _SOURCE_ROOT + "/alpha/agent/decisions",
    _SOURCE_ROOT + "/alpha/loop",
    _SOURCE_ROOT + "/analytics",
    _SOURCE_ROOT + "/analytics/decisions",
]

def _initializer_import(dep, package):
    label = Label("//" + package + dep if dep.startswith(":") else dep)
    if not label.repo_name and (label.package == _SOURCE_ROOT or label.package.startswith(_SOURCE_ROOT + "/")):
        return str(label.relative(":" + label.name + "_init_import"))
    return dep

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
    deps = deps + extra_deps
    package = native.package_name()
    if package != _SOURCE_ROOT and not package.startswith(_SOURCE_ROOT + "/"):
        py_library(name = name, deps = deps, **kwargs)
        return

    initializer = name == "__init__"
    package_initializers = [
        parent
        for parent in _PACKAGE_INITIALIZERS
        if package == parent or package.startswith(parent + "/")
    ]
    if initializer and package in _PACKAGE_INITIALIZERS:
        py_library(
            name = "package_init_source",
            srcs = kwargs.pop("srcs"),
            imports = kwargs.pop("imports"),
            visibility = kwargs["visibility"],
        )

    # Imports made during initialization need the initializer files and the
    # explicit import graph. They cannot also depend on completion of the
    # initializer that is importing them.
    init_import = name + "_init_import"
    py_library(
        name = init_import,
        deps = [_initializer_import(dep, package) for dep in deps] +
               ["//" + parent + ":package_init_source" for parent in package_initializers],
        **kwargs
    )

    package_deps = ["//" + parent + ":__init__" for parent in package_initializers if not initializer or parent != package]
    if initializer:
        deps = []
    py_library(name = name, deps = deps + [":" + init_import] + package_deps, visibility = kwargs["visibility"])
