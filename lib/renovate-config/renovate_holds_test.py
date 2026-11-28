# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Keep the two deliberate Renovate holds in default.json5."""

import unittest
from pathlib import Path

from renovate_test_helpers import parse_preset

ROOT = Path(__file__).parent


def rules():
    return parse_preset((ROOT / "default.json5").read_text())["packageRules"]


def find(**keys):
    """Return the rules that carry every given key and value."""
    return [r for r in rules() if all(r.get(k) == v for k, v in keys.items())]


class HoldsTest(unittest.TestCase):
    def test_tofu_required_version_disabled(self):
        found = find(
            matchManagers=["terraform"],
            matchFileNames=["ops/tofu/**"],
            matchDepTypes=["required_version"],
        )
        self.assertEqual(len(found), 1, found)
        self.assertIs(found[0].get("enabled"), False)

    def test_typescript_held_below_7(self):
        found = find(
            matchManagers=["npm"],
            matchPackageNames=["typescript"],
            matchFileNames=[
                "lib/rules/astro/package.json",
                "lib/rules/typescript/package.json",
            ],
        )
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0].get("allowedVersions"), "<7")


if __name__ == "__main__":
    unittest.main()
