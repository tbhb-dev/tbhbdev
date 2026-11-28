"""The host entry point for a bundled agent run."""

from __future__ import annotations

import sys
from importlib import metadata

import tbhbdev
from tbhbdev import version
from tbhbdev.alpha.agent.bundle import arguments
from tbhbdev.alpha.agent.run_host import dispatcher


def main(argv: list[str], prog: str) -> int:
    """Start the dispatcher with this build's record for `NAME.build`.

    The dispatcher does not accept `--cli-sha256`, so it gets ARGV without it
    and writes the build record under its run lock after duplicate-run checks.
    """
    digest, rest = arguments.split_digest(argv)
    if arguments.run_name(rest) is None:
        # The dispatcher reports the missing name.
        return dispatcher.main(rest, prog)
    if digest is None:
        print(f"{prog}: {arguments.DIGEST_OPTION} is required", file=sys.stderr)
        return 1
    record = arguments.record(_commit(), tbhbdev.__file__, digest)
    return dispatcher.main(rest, prog, build=record)


def _commit() -> str:
    """The build this process imports, from its install record or its stamp."""
    try:
        text = metadata.distribution(version.NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        text = None
    return version.identity(text, version.build_module())
