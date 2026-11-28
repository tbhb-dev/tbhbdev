# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Unit tests for tags on the Ansible check targets."""

load("@rules_testing//lib:unit_test.bzl", "unit_test")

def slow_external_tags_test(name, targets):
    """Checks that each named target in this package carries the slow-external tag.

    Args:
        name: Name of the test target.
        targets: Target names in the current package that must carry slow-external.
    """
    tags_by_target = {
        target: list(native.existing_rule(target).get("tags", []))
        for target in targets
    }

    def impl(env):
        for tags in tags_by_target.values():
            env.expect.that_collection(tags).contains("slow-external")

    unit_test(name = name, impl = impl)

def timeout_fits_load_test(name, targets):
    """Checks that each named target in this package has the eternal timeout.

    The repository's `--test_timeout` mapping gives `eternal` 900 s, so the
    lint watchdog, at 80% of it, stops ansible-lint at 720 s. A target that
    leaves the timeout unset is `moderate`, 180 s here, which a loaded CI
    runner exceeds.

    Args:
        name: Test target name.
        targets: Target names in the current package that must set `timeout = "eternal"`.
    """
    timeout_by_target = {
        target: native.existing_rule(target).get("timeout")
        for target in targets
    }

    def impl(env):
        for target, timeout in timeout_by_target.items():
            env.expect.where(target = target).that_str(timeout).equals("eternal")

    unit_test(name = name, impl = impl)
