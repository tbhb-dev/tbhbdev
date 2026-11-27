# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Parse the JSON5 subset used by the Renovate tests."""

import json
import re

# Match strings first, preserving comment markers and commas inside strings.
TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def parse_preset(source):
    """Read a preset without changing string values."""
    source = TOKENS.sub(lambda m: m[0] if m[0].startswith('"') else "", source)
    # Transform only the gaps between strings, where keys and commas occur.
    parts = re.split(r'("(?:\\.|[^"\\])*")', source)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"([A-Za-z_$][\w$]*)\s*:", r'"\1":', parts[index])
        parts[index] = re.sub(r",\s*([}\]])", r"\1", parts[index])
    return json.loads("".join(parts))
