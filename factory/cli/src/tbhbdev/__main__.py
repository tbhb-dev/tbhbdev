"""`python -m tbhbdev`: the same entry point as the `tbhbdev` command."""

# gazelle:include_dep //factory/cli/src/tbhbdev/alpha:command_modules

import sys

from tbhbdev.cli import main

sys.exit(main())
