# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Connect Gazelle's per-file test rules to pytest's module entry point."""

load("@rules_python//python:defs.bzl", "py_library", "py_test")

_IMPORTED_TESTS = [
    "test_admin_request",
    "test_alpha_agent_codex_install",
    "test_alpha_agent_credentials",
    "test_alpha_agent_git_parent",
    "test_alpha_agent_request",
    "test_alpha_agent_run_host",
    "test_alpha_agent_sandbox_live",
    "test_alpha_agent_settings",
    "test_alpha_loop_rules",
    "test_alpha_loop_store",
    "test_alpha_pull_request",
    "test_alpha_review_doc",
]

def cli_py_test(name, srcs, deps = [], data = [], extra_deps = [], pyi_deps = [], path_tools = [], **kwargs):
    """Run pytest on this file, and expose importable test helpers separately.

    Args:
        name: Bazel test name.
        srcs: Test source from Gazelle.
        deps: Runtime imports from Gazelle.
        data: Non-Python test inputs.
        extra_deps: Imports used by subprocesses or package initializers.
        pyi_deps: Type-only imports from Gazelle.
        path_tools: Tools this test finds on PATH, in addition to CUE.
        **kwargs: Additional py_test attributes.
    """
    _ = pyi_deps
    deps = deps + extra_deps
    if name in _IMPORTED_TESTS:
        py_library(
            name = name + "_support",
            srcs = srcs,
            data = data,
            deps = deps,
        )

    tools = ["@cue_test_tool//:cue"] + path_tools
    py_test(
        name = name,
        srcs = srcs,
        args = [
            "-c",
            "factory/cli/pyproject.toml",
            "-p",
            "no:cacheprovider",
            "-p",
            "bazel_test_setup",
            "factory/cli/tests/" + srcs[0],
        ],
        data = data + tools + ["//factory/cli:pyproject.toml"],
        deps = deps + [":bazel_test_setup", ":sitecustomize", "@cli_pip//pytest_cov"],
        env = {
            "CLI_TEST_TOOLS": " ".join(["$(rootpath %s)" % tool for tool in tools]),
        },
        imports = ["."],
        legacy_create_init = False,
        main_module = "pytest",
        **kwargs
    )
