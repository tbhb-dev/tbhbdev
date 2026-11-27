# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Run a Gazelle-generated Python test suite with pytest."""

load("@rules_python//python:defs.bzl", "py_test")

def ops_py_test(name, data = [], **kwargs):
    """Run the suite with the pytest configuration of its Python distribution.

    Args:
        name: Bazel test name.
        data: Non-Python fixtures for the suite.
        **kwargs: Additional py_test attributes.
    """
    package = native.package_name()
    project = package.split("/tests")[0]
    py_test(
        name = name,
        args = ["-c", project + "/pyproject.toml", "-p", "no:cacheprovider", package],
        data = data + ["//ops/packages:" + project.removeprefix("ops/packages/") + "/pyproject.toml"],
        legacy_create_init = False,
        main_module = "pytest",
        **kwargs
    )
