"""The host entry point for a bundled agent run."""

from __future__ import annotations

import sys
from importlib import metadata

import tbhbdev
from tbhbdev import version
from tbhbdev.alpha.agent.bundle import arguments
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.agent.run_host import dispatcher, intake


def main(argv: list[str], prog: str) -> int:
    """Record this build in `NAME.build`, then start the dispatcher.

    The dispatcher does not accept `--cli-sha256`, so it gets ARGV without it.
    """
    digest, rest = arguments.split_digest(argv)
    name = arguments.run_name(rest)
    if name is None:
        # The dispatcher reports the missing name.
        return dispatcher.main(rest, prog)
    if digest is None:
        print(f"{prog}: {arguments.DIGEST_OPTION} is required", file=sys.stderr)
        return 1
    try:
        intake.files_for(name).write(
            "build", arguments.record(_commit(), tbhbdev.__file__, digest)
        )
    except (Invalid, OSError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    return dispatcher.main(rest, prog)


def _commit() -> str:
    """The build this process imports, from its install record or its stamp."""
    try:
        text = metadata.distribution(version.NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        text = None
    return version.identity(text, version.build_module())
