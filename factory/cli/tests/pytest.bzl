# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Connect Gazelle's per-file test rules to pytest's module entry point."""

load("@rules_python//python:defs.bzl", "py_library", "py_test")

_TEST_ROOT = "factory/cli/tests"

_IMPORTED_TESTS = [
    "test_admin_request",
    "test_alpha_agent_codex_install",
    "test_alpha_agent_credentials",
    "test_alpha_agent_git_parent",
    "test_alpha_agent_request",
    "test_alpha_agent_run_host",
    "test_alpha_agent_sandbox_live",
    "test_alpha_agent_settings",
    "test_alpha_pull_request",
    "test_alpha_review_doc",
]

# Existing tests that fail with the I/O guard on. Remove entries here and in
# io_test_baseline.txt as tests become isolated. Live and I/O filenames are
# recognized separately.
_IO_TESTS = [
    "test_agy_otel",
    "test_alpha_agent_credentials",
    "test_alpha_agent_git_parent",
    "test_alpha_agent_ownership",
    "test_alpha_agent_policy",
    "test_alpha_agent_precheck",
    "test_alpha_agent_preparation",
    "test_alpha_agent_process_group",
    "test_alpha_agent_push",
    "test_alpha_agent_review_post",
    "test_alpha_agent_run_host",
    "test_alpha_agent_runfiles",
    "test_alpha_agent_signing_parity",
    "test_alpha_agent_ssh_serve",
    "test_alpha_agent_terminal_descriptors",
    "test_alpha_agent_transport",
    "test_alpha_agent_worktree",
    "test_alpha_close",
    "test_alpha_gh_shim",
    "test_alpha_guard_gh_poll",
    "test_alpha_guard_gh_pr",
    "test_alpha_guard_ssh_agent",
    "test_alpha_monorepo",
    "test_alpha_opened",
    "test_alpha_pre_push_guard",
    "test_alpha_pull_request",
    "test_alpha_repo",
    "test_alpha_repo_token",
    "test_alpha_review",
    "test_alpha_review_doc",
    "test_alpha_review_prompt",
    "test_alpha_run_stop_hook",
    "test_alpha_secret_scan",
    "test_alpha_size_gate",
    "test_alpha_verify_range",
    "test_cli",
    "test_conftest",
    "test_content_review",
    "test_content_vet",
    "test_cue_runtime",
    "test_gitleaks_check",
    "test_host",
    "test_secret",
    "test_secrets",
    "test_telemetry",
]

def cli_py_test(name, srcs, deps = [], data = [], extra_deps = [], pyi_deps = [], path_tools = [], tags = [], imports = ["."], **kwargs):
    """Run pytest on this file, and expose importable test helpers separately.

    Args:
        name: Bazel test name.
        srcs: Test source from Gazelle.
        deps: Runtime imports from Gazelle.
        data: Non-Python test inputs.
        extra_deps: Imports used by subprocesses or package initializers.
        pyi_deps: Type-only imports from Gazelle.
        path_tools: Tools this test finds on PATH, in addition to CUE.
        tags: Additional tags for the pytest target.
        imports: Python import roots relative to the test package.
        **kwargs: Additional py_test attributes.
    """
    _ = pyi_deps
    deps = deps + extra_deps
    package = native.package_name()
    filename = srcs[0]
    io_test = (
        (package + "/").startswith(_TEST_ROOT + "/integration/") or
        name in _IO_TESTS or
        filename.endswith("_io.py") or
        "_live" in filename
    )
    tags = tags + (["io"] if io_test else ["block-network"])
    if name in _IMPORTED_TESTS:
        # Rules that precompile the same source must share execution requirements.
        py_library(
            name = name + "_support",
            srcs = srcs,
            data = data,
            deps = deps,
            tags = tags,
        )

    tools = ["@cue_test_tool//:cue"] + path_tools
    env = {
        "CLI_TEST_TOOLS": " ".join(["$(rootpath %s)" % tool for tool in tools]),
    }
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
        ] + ([] if io_test else ["-p", "bazel_test_io"]) + [
            package + "/" + filename,
        ],
        data = data + tools + ["//factory/cli:pyproject.toml"],
        deps = deps + ["//" + _TEST_ROOT + ":bazel_test_setup", "//" + _TEST_ROOT + ":sitecustomize", "@cli_pip//pytest_cov"] + ([] if io_test else ["//" + _TEST_ROOT + ":bazel_test_io"]),
        env = env,
        imports = imports,
        legacy_create_init = False,
        main_module = "pytest",
        tags = tags,
        **kwargs
    )
