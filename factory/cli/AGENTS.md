# Agent instructions

This project holds shared tools for tbhb-dev work. Every tool is a subcommand of the `tbhbdev` Python package.

## Standing rules

- Never write a public IP address in a commit, a pull request, or a report. Refer to the host by its name instead. The `public-ipv4` gitleaks rule enforces this.
- Secrets live in 1Password only. Never paste one into chat or commit one to git.
- A tool that one repository needs for its own checks or releases belongs with that project or in the shared rules at `lib/rules`.

## Models

Before choosing a model for a subagent or peer run, read models/README.md in tbhb-dev/wiki (<https://github.com/tbhb-dev/wiki/blob/main/models/README.md>).

## Commits and pull requests

- Branch first. Every change lands through a pull request.
- Use Conventional Commits and a plain `git commit`, without `Signed-off-by` or `Co-authored-by`, and draft the pull request in `.github/PULL_REQUEST_DRAFT.md` using `.github/PULL_REQUEST_TEMPLATE.md`. Keep the draft out of commits.

## Adding a tool

1. Write the tool as `src/tbhbdev/<tool>.py`, or as a package directory under `src/tbhbdev/` for a larger one, with a `main(argv, prog)` that returns the exit status.
2. Register it in `TOOLS` in `src/tbhbdev/cli.py` under its command name.
3. Add `tests/test_<tool>.py`. The tests keep the package at 100% branch coverage. The nightly mutation job reports surviving mutants for follow-up.
4. List it in the README.

## Alpha commands

`tbhbdev alpha` holds the interim agent tools, such as the agent identity hooks and the pull request commands `tbhbdev alpha pr` and `tbhbdev alpha merge`. Mark a pull request ready with `tbhbdev alpha pr ready N`, because the hooks refuse raw `gh pr ready`. They may change without notice. The harness hooks are user level, not in this repository: install `tbhbdev` with `uv tool install "git+https://github.com/tbhb-dev/tbhbdev#subdirectory=factory/cli"`, then run `tbhbdev alpha install` in a clone. See [Install](docs/reference/alpha/commands/install.md) for what it writes.

Ask Renovate to rebase an open pull request with `tbhbdev alpha pr rebase N`.

Document a new or changed command in its own page under `docs/reference/alpha/commands/`, and add one entry to the index `docs/reference/alpha/commands/index.md`, not to `docs/reference/alpha/commands.md`.

## Checks

From the monorepo root, run `uv sync --all-packages` to set up the workspace and `bazelisk test //factory/cli:pytest_test` to check the whole CLI suite. The other Python gates are in this project's `BUILD.bazel`.
