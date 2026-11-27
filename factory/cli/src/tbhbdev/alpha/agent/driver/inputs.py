"""Read launch inputs and report validation results."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import telemetry
from tbhbdev.alpha.agent.driver import version_heal
from tbhbdev.alpha.agent.driver.decisions.prompt import (
    check_brief,
    check_schema,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    import argparse


def checked_prompt(args: argparse.Namespace) -> str:
    prompt = Path(args.prompt_file).read_text()
    if not prompt and version_heal.retrying():
        message = f"{args.prompt_file}: brief is empty after the driver restarted"
        raise Invalid(message)
    try:
        warnings = check_brief(prompt, args)
    except Invalid:
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        raise
    for warning in warnings:
        print(f"warning: {args.prompt_file}: {warning}", file=sys.stderr)
    return prompt


def checked_schema(args: argparse.Namespace) -> str | None:
    if args.schema is None:
        return None
    schema = Path(args.schema).read_text()
    try:
        check_schema(schema)
    except Invalid:
        telemetry.attributes(**{"tbhb.agent.outcome": "refused"})
        raise
    return schema
