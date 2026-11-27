# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Check commands in the selected presets against the workflow's allowlist."""

import json
import re
import unittest
from pathlib import Path

from renovate_test_helpers import parse_preset

PRESETS = Path(__file__).parent
ROOT = PRESETS.parents[1]
PRESET_PREFIX = "github>tbhb-dev/tbhbdev//lib/renovate-config/"


def allowed_patterns(source):
    """Extract the single-quoted folded YAML scalar and decode its JSON list."""
    matches = re.findall(
        r"^          RENOVATE_ALLOWED_COMMANDS: '((?:[^']|'')*)'\s*$",
        source,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("Expected one single-quoted RENOVATE_ALLOWED_COMMANDS value")
    value = re.sub(r"\n[ \t]*", " ", matches[0]).replace("''", "'")
    patterns = json.loads(value)
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("RENOVATE_ALLOWED_COMMANDS must be a list of strings")
    return patterns


def check_allowlist(commands, patterns):
    """Return every command coverage and unused-pattern failure."""
    errors = []
    for command in commands:
        count = sum(re.fullmatch(pattern, command) is not None for pattern in patterns)
        if count != 1:
            errors.append(f"Command {command!r} matches {count} regular expressions")
    for pattern in patterns:
        if not any(re.fullmatch(pattern, command) is not None for command in commands):
            errors.append(f"Regular expression {pattern!r} matches no command")
    return errors


class RenovateAllowlistTest(unittest.TestCase):
    def test_repository(self):
        root_config = parse_preset((ROOT / "renovate.json5").read_text())
        commands = []
        for preset in root_config["extends"]:
            if not preset.startswith(PRESET_PREFIX):
                continue
            preset_path = PRESETS / preset.removeprefix(PRESET_PREFIX)
            config = parse_preset(preset_path.read_text())
            for rule in config.get("packageRules", []):
                commands.extend(rule.get("postUpgradeTasks", {}).get("commands", []))
        patterns = allowed_patterns(
            (ROOT / ".github/workflows/renovate.yaml").read_text()
        )
        self.assertTrue(commands, "No post-upgrade commands found")
        self.assertEqual(check_allowlist(commands, patterns), [])

    def test_negative_fixtures(self):
        for commands, patterns, detail in (
            (["unsafe"], ["^allowed$"], "Command 'unsafe' matches 0"),
            (["allowed"], ["^allowed$", "^unused$"], "'matches no command'"),
            (["allowed"], ["^allowed$", "^allow.*$"], "Command 'allowed' matches 2"),
        ):
            with self.subTest(commands=commands, patterns=patterns):
                errors = check_allowlist(commands, patterns)
                self.assertTrue(errors)
                if "matches no command" in detail:
                    self.assertIn(
                        "Regular expression '^unused$' matches no command", errors
                    )
                else:
                    self.assertTrue(any(detail in error for error in errors), errors)

    def test_json5_strings_survive(self):
        config = parse_preset("""// comment
        {url: "https://example.test/a,]", commands: ["run //:target",],}
        """)
        self.assertEqual(
            config, {"url": "https://example.test/a,]", "commands": ["run //:target"]}
        )


if __name__ == "__main__":
    unittest.main()
