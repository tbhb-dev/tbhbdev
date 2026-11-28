# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Check the pytest file assignments in this Bazel package."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")

def _partition_test_impl(ctx):
    env = unittest.begin(ctx)
    for error in ctx.attr.errors:
        asserts.true(env, False, error)
    return unittest.end(env)

_partition_test = unittest.make(_partition_test_impl, attrs = {"errors": attr.string_list()})

def pytest_partition_test(name):
    """Check that each test module is collected by exactly one preceding target.

    Args:
        name: Bazel test name.
    """
    modules = native.glob(["**/test_*.py", "**/*_test.py"], allow_empty = True)
    package = native.package_name()
    assignments = {module: [] for module in modules}
    errors = []
    for target, rule in native.existing_rules().items():
        if rule["kind"] != "py_test" or rule.get("main_module") != "pytest":
            continue
        sources = [source.removeprefix(":") for source in rule["srcs"]]
        paths = rule["args"]
        for module in modules:
            in_sources = module in sources
            selected = package + "/" + module in paths or (package in paths and in_sources)
            if in_sources != selected:
                errors.append("{} must list {} in both srcs and pytest paths".format(target, module))
            if selected:
                assignments[module].append(target)

    for module, targets in assignments.items():
        if len(targets) != 1:
            errors.append("{} belongs to {} pytest targets: {}".format(module, len(targets), ", ".join(targets) or "none"))
    _partition_test(name = name, errors = errors, size = "small")
