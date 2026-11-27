"""The `tbhbdev` command: one subcommand per tool.

Each tool is a module of this package with a `main(argv, prog)` that returns
its exit status. `tbhbdev <tool> ARGS...` passes ARGS to it unchanged, with
`tbhbdev <tool>` as the program name in its messages.
"""

import sys
from collections.abc import Callable, Mapping, Sequence

from tbhbdev import (
    adr,
    agy,
    cue_runtime,
    issue,
    secret,
    version,
)
from tbhbdev.admin import command as admin_command
from tbhbdev.alpha import command as alpha_command
from tbhbdev.analytics import command as analytics_command

Tool = Callable[[list[str], str], int]

TOOLS: dict[str, Tool] = {
    "admin": admin_command.main,
    "adr": adr.main,
    "agy": agy.main,
    "alpha": alpha_command.main,
    "analytics": analytics_command.main,
    "cue": cue_runtime.main,
    "issue": issue.main,
    "secret": secret.main,
}


def usage(tools: Mapping[str, Tool]) -> str:
    return (
        "usage: tbhbdev <tool> [args...]\n       tbhbdev --version\n\ntools:\n"
        + "".join(f"  {name}\n" for name in sorted(tools))
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tool argv names, passing it the rest of argv."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-V", "--version"}:
        sys.stdout.write(version.line() + "\n")
        return 0
    if not args or args[0] in {"-h", "--help"}:
        stream = sys.stdout if args else sys.stderr
        stream.write(usage(TOOLS))
        return 0 if args else 2
    name, rest = args[0], args[1:]
    tool = TOOLS.get(name)
    if tool is None:
        sys.stderr.write(f"tbhbdev: unknown tool {name!r}\n{usage(TOOLS)}")
        return 2
    return tool(rest, f"tbhbdev {name}")
