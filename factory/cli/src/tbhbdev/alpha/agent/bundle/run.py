"""The host entry point for a bundled agent run."""

from tbhbdev.alpha.agent.model import Invalid

_NOT_BUILT = "not built"


def main(argv: list[str], prog: str) -> int:
    del argv, prog
    raise Invalid(_NOT_BUILT)
