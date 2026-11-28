"""The driver entry point for a bundled agent launch."""

from tbhbdev.alpha.agent.model import Invalid


def main(argv: list[str], prog: str) -> int:
    del argv, prog
    message = "not built"
    raise Invalid(message)
