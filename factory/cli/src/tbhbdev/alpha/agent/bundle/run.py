"""The host entry point for a bundled agent run."""

from __future__ import annotations

import sys
from contextlib import suppress
from importlib import metadata

import tbhbdev
from tbhbdev import version
from tbhbdev.alpha.agent.bundle import arguments
from tbhbdev.alpha.agent.decisions import runfiles_names
from tbhbdev.alpha.agent.model import NAME
from tbhbdev.alpha.agent.run_host import bazel_cache, dispatcher, intake


def main(argv: list[str], prog: str) -> int:
    """Start the dispatcher with this build's record for `NAME.build`.

    The dispatcher does not accept `--cli-sha256`, so it gets ARGV without it
    and writes the build record under its run lock after duplicate-run checks.
    """
    digest, rest = arguments.split_digest(argv)
    name = arguments.run_name(rest)
    if name is None:
        # The dispatcher reports the missing name.
        return dispatcher.main(rest, prog)
    if digest is None:
        print(f"{prog}: {arguments.DIGEST_OPTION} is required", file=sys.stderr)
        return 1
    record = arguments.record(_commit(), tbhbdev.__file__, digest)
    result = dispatcher.main(rest, prog, build=record)
    if NAME.fullmatch(name):
        # A filesystem error during cleanup must preserve the dispatcher's result.
        with suppress(OSError):
            files = intake.files_for(name)
            if files.path("rc").is_file(follow_symlinks=False):
                cli = files.root / f"{name}{runfiles_names.CLI}"
                bazel_cache.remove_owned_dir(files.root.parent.parent, cli)
    return result


def _commit() -> str:
    """The build this process imports, from its install record or its stamp."""
    try:
        text = metadata.distribution(version.NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        text = None
    return version.identity(text, version.build_module())
