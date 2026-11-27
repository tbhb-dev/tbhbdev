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
