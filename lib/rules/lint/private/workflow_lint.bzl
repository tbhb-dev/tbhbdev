# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""`workflow_lint_test`: pinned actions, fixed runner labels, no public self-hosted runners."""

load(":tree_check.bzl", "tree_check_test")

# Internal: consumers load //lint:defs.bzl.
visibility("//lint/...")

def workflow_lint_test(name, srcs, private = False, paths = [".github"], **kwargs):
    """Checks workflow and action YAML for the rules zizmor does not enforce alone.

    Requires full commit SHAs, SHA-256 Docker image digests, and local action
    directories under `.github/actions/`. Also rejects floating runner labels
    and public self-hosted runners. The check is independent of zizmor config.

    Args:
        name: Target name.
        srcs: Files to check, including `.github/`.
        private: True when the repository is private. Only then may a workflow name `self-hosted`.
        paths: Files or directories to scan, relative to the tree root.
        **kwargs: Passed to tree_check_test.
    """
    tree_check_test(
        name = name,
        srcs = srcs,
        tool = Label(":workflow_lint.sh"),
        tool_args = (["--private"] if private else []) + paths,
        output_guard = "workflow_lint: scanned [1-9]",
        **kwargs
    )
