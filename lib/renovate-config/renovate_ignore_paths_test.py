# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Keep Renovate away from fixtures that are pinned on purpose."""

import unittest
from pathlib import Path

from renovate_test_helpers import parse_preset

ROOT = Path(__file__).parent

# Each entry guards a proof that fails or passes only at the pinned version.
REQUIRED = {
    # proof_lock_mismatch needs requirements.yml to differ from the lock.
    "**/planted/**",
    # The GO-2021-0113 proofs need the vulnerable golang.org/x/text v0.3.6.
    "lib/rules/go/e2e/smoke/govulncheck/**",
}


class IgnorePathsTest(unittest.TestCase):
    def test_pinned_fixtures_are_ignored(self):
        ignored = parse_preset((ROOT / "default.json5").read_text())["ignorePaths"]
        self.assertEqual(sorted(REQUIRED - set(ignored)), [])

    def test_one_list_only(self):
        # ignorePaths is not merged across presets, so a second copy in a
        # sibling preset would replace this list in a consumer.
        for preset in sorted(ROOT.glob("*.json5")):
            if preset.name == "default.json5":
                continue
            self.assertNotIn("ignorePaths", parse_preset(preset.read_text()), preset)


if __name__ == "__main__":
    unittest.main()
