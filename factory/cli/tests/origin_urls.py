"""Origin URLs for the tbhb-dev origin check, shared by its tests.

test_alpha_repo checks repo.is_tbhb_dev_url against these, and the identity
installer's parity test runs the shim's shell pattern over
the same rows, so the two checks must agree on every one.
"""

TBHB_DEV_URLS = [
    "git@github.com:tbhb-dev/tbhbdev.git",
    "git@github.com:tbhb-dev/tbhbdev",
    "git@github.com:tbhb-dev/tbhbdev/",
    "git@github.com:TBHB-DEV/Tbhbdev.git",
    "org-1@github.com:tbhb-dev/tbhbdev.git",
    "ssh://git@github.com/tbhb-dev/tbhbdev.git",
    "ssh://git@github.com:22/tbhb-dev/tbhbdev.git",
    "ssh://git@ssh.github.com:443/tbhb-dev/tbhbdev.git",
    "git+ssh://git@github.com/tbhb-dev/tbhbdev.git",
    "https://github.com/tbhb-dev/tbhbdev.git",
    "https://github.com/tbhb-dev/tbhbdev",
    "https://github.com/tbhb-dev/tbhbdev/",
    "https://x-access-token@github.com/tbhb-dev/tbhbdev.git",
    "HTTPS://GitHub.com/tbhb-dev/tbhbdev",
    "  https://github.com/tbhb-dev/tbhbdev\n",
    "\nhttps://github.com/tbhb-dev/tbhbdev\t",
    "https://github.com/tbhb-dev/tbhbd\u00e9v",
]
OTHER_URLS = [
    "",
    "git@github.com:someone/tools.git",
    "git@github.com:tbhb-dev-fork/tools.git",
    "git@github.com:tbhb-dev-evil/tools.git",
    "https://github.com/tbhb-dev-evil/tools",
    "git@github.com:xtbhb-dev/tbhbdev.git",
    "git@github.com:tbhb-dev/",
    "git@github.com:tbhb-dev/a/b",
    "git@gitlab.com:tbhb-dev/tbhbdev.git",
    "git@github.com.evil.example:tbhb-dev/tbhbdev.git",
    "git@evil.example:github.com:tbhb-dev/tbhbdev.git",
    "https://github.com.evil.example/tbhb-dev/tbhbdev",
    "https://evil.example/github.com/tbhb-dev/tbhbdev",
    "http://github.com/tbhb-dev/tbhbdev",
    "https://github.com/someone/tbhb-dev",
    "https://github.com/tbhb-dev/tbhbdev other",
    "ssh://git@github.com/someone/tbhb-dev/tbhbdev",
    "/srv/git/tbhb-dev/tbhbdev.git",
    "file:///github.com/tbhb-dev/tbhbdev",
    "github.com/tbhb-dev/tbhbdev",
    "git@github.com:tbhb-dev/tbhbdev.git\nx",
    "x\ngit@github.com:tbhb-dev/tbhbdev.git",
    "ssh://git@github.com:2a/tbhb-dev/tbhbdev.git",
    # Only ASCII counts: a non-ASCII digit, space or case fold doesn't match.
    "ssh://git@github.com:\u0662\u0662/tbhb-dev/tbhbdev.git",
    "\u00a0https://github.com/tbhb-dev/tbhbdev",
    "http\u017f://github.com/tbhb-dev/tbhbdev",
]
