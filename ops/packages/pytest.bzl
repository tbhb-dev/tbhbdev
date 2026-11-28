# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Run a Gazelle-generated Python test suite with pytest."""

load("@rules_python//python:defs.bzl", "py_test")

def ops_py_test(name, data = [], paths = None, **kwargs):
    """Run the suite with the pytest configuration of its Python distribution.

    Args:
        name: Bazel test name.
        data: Non-Python fixtures for the suite.
        paths: Package-relative test files. Defaults to the whole package.
        **kwargs: Additional py_test attributes.
    """
    package = native.package_name()
    project = package.split("/tests")[0]
    test_paths = [package + "/" + path for path in paths] if paths != None else [package]
    py_test(
        name = name,
        args = ["-c", project + "/pyproject.toml", "-p", "no:cacheprovider"] + test_paths,
        data = data + ["//ops/packages:" + project.removeprefix("ops/packages/") + "/pyproject.toml"],
        legacy_create_init = False,
        main_module = "pytest",
        **kwargs
    )
