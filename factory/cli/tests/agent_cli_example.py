"""Invoke the agent command through the public CLI."""

from tbhbdev import cli


def run_agent(argv: list[str]) -> int:
    return cli.main(["alpha", "agent", *argv])
