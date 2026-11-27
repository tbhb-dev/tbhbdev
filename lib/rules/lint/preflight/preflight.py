# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import argparse
import asyncio
import errno
import os
import shlex
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

DEFAULT_BASE = "origin/main"
CI_CHECKS = Path("lib/rules/lint/ci_checks.sh")
AFFECTED = Path(".github/actions/affected-targets")
DRAFT = Path(".github/PULL_REQUEST_DRAFT.md")
BAZEL_TEST = ("bazelisk", "test", "--config=ci")
BAZEL_RUN = ("bazelisk", "run", "--config=ci")
FIXED_COMMANDS = {
    "gazelle": ("bazelisk", "run", "//:gazelle", "--", "-mode=diff"),
    "actionlint": (*BAZEL_TEST, "//:actionlint_test"),
    "gitleaks-history": (
        *BAZEL_RUN,
        "//:gitleaks_history",
        "--",
        "--log-opts=--diff-merges=first-parent HEAD",
    ),
    "github-metrics-vulnerabilities": (
        *BAZEL_TEST,
        "//factory/github-metrics:govulncheck_test",
    ),
    "agent-capacity-images": (
        *BAZEL_TEST,
        "-c",
        "opt",
        "//factory/agent-capacity/container:image_multiarch",
        "//factory/agent-capacity/container:image_config_test",
    ),
}
ALWAYS = (
    "lint-baselines",
    *FIXED_COMMANDS,
    "pr-size",
    "design-report",
    "prose",
    "agent-capacity-vulnerabilities",
    "agent-capacity-modules",
    "module-tidy",
)


def checks_for(paths: list[str], *, has_draft: bool) -> tuple[str, ...]:
    affected = ("affected-tests", "cli-coverage") if paths else ()
    text = ("gitleaks-text",) if has_draft else ()
    return (*affected, *text, *ALWAYS)


def summary(results: list[tuple[str, bool]], base: str) -> str:
    lines = []
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        command = f"bazelisk run //:preflight -- --check {shlex.join((name, base))}"
        lines.append(f"{status} {name}  {command}")
    return "\n".join(lines)


class CheckError(Exception):
    """A check command exited unsuccessfully."""


class Revisions(NamedTuple):
    base: str
    merge_base: str
    head: str
    ref: str


async def run(
    *command: str | Path,
    seconds: int = 1500,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    capture: bool = False,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE if capture else None,
        start_new_session=sys.platform != "win32",
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(stdin), seconds)
    except TimeoutError, asyncio.CancelledError:
        if process.returncode is None:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
        raise
    if process.returncode:
        message = f"{shlex.join(map(str, command))} exited {process.returncode}"
        raise CheckError(message)
    return output or b""


async def git(*args: str) -> str:
    return os.fsdecode(await run("git", *args, capture=True)).rstrip("\n")


async def affected_tests(base: str, head: str, scratch: Path) -> None:
    determinator = scratch / "target-determinator"
    output = scratch / "selection"
    try:
        await run("bash", AFFECTED / "download.sh", determinator, seconds=90)
    except CheckError, TimeoutError:
        determinator = None
    checkout = scratch / "checkout"
    await run("git", "clone", "--quiet", "--shared", "--no-checkout", ".", checkout)
    await run("git", "checkout", "--quiet", "--detach", head, cwd=checkout)
    await run(
        "bash",
        AFFECTED / "select.sh",
        seconds=1020,
        cwd=checkout,
        env={
            **os.environ,
            "EVENT": "pull_request",
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "TD": str(determinator or ""),
            "RUNNER_TEMP": str(scratch),
            "GITHUB_OUTPUT": str(output),
            "BAZEL_OPTS": "--config=ci",
        },
    )
    selection = await asyncio.to_thread(output.read_text)
    target_file = next(
        (line[5:] for line in selection.splitlines() if line.startswith("file=")), ""
    )
    if not target_file:
        raise OSError(errno.EIO, "affected-targets did not report a target file")
    if (await asyncio.to_thread(Path(target_file).stat)).st_size:
        await run(
            *BAZEL_TEST, "--build_tests_only", f"--target_pattern_file={target_file}"
        )


async def check(name: str, revisions: Revisions, scratch: Path) -> None:
    base, merge_base, head, ref = revisions
    if name in FIXED_COMMANDS:
        await run(*FIXED_COMMANDS[name])
        return
    script = Path.cwd() / CI_CHECKS
    match name:
        case "affected-tests":
            await affected_tests(merge_base, head, scratch)
        case "cli-coverage":
            await run(*BAZEL_RUN, "//factory/cli:coverage")
        case "lint-baselines":
            await run(
                *BAZEL_RUN, "@tbhb_rules//lint:lint_baselines", "--", merge_base, head
            )
        case "gitleaks-text":
            await run(
                *BAZEL_RUN,
                "@tbhb_rules//lint:gitleaks_text",
                stdin=await asyncio.to_thread(DRAFT.read_bytes),
            )
        case "module-tidy" | "prose":
            prefix = ("mise", "exec", "--") if name == "prose" else ()
            await run(*prefix, "bash", script, name, scratch)
        case "agent-capacity-vulnerabilities":
            await run(
                *BAZEL_RUN,
                "//factory/agent-capacity:govulncheck",
                "--",
                f"--sarif={scratch}/agent-capacity.sarif",
            )
        case "agent-capacity-modules":
            scanner, vendor = scratch / "gomodscan", scratch / "vendored"
            await run("bash", script, "scanner-checkout", scanner, seconds=120)
            await run("bash", script, "vendor", vendor)
            await run(
                "bash", script, "modules", scanner, vendor, scratch / "gomodscan.sarif"
            )
        case "pr-size" | "design-report":
            checkout = scratch / "base"
            await run("git", "worktree", "add", "--quiet", "--detach", checkout, base)
            try:
                if name == "pr-size":
                    args = (
                        base,
                        head,
                        ref,
                        DEFAULT_BASE,
                        await git("branch", "--show-current"),
                    )
                    await run("bash", script, name, *args, cwd=checkout)
                    return
                await run(
                    "bash",
                    script,
                    name,
                    checkout,
                    scratch,
                    env={**os.environ, "PYTHON": sys.executable},
                )
            finally:
                await run("git", "worktree", "remove", "--force", checkout)
        case _:
            raise OSError(errno.EINVAL, "unknown check", name)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the CI checks at the current commit."
    )
    parser.add_argument("base", nargs="?", default=DEFAULT_BASE)
    parser.add_argument("--check", choices=checks_for(["BUILD.bazel"], has_draft=True))
    args = parser.parse_args()
    os.chdir(os.environ.get("BUILD_WORKSPACE_DIRECTORY", "."))
    if await git("status", "--porcelain", "--untracked-files=no"):
        parser.error("commit tracked changes before checking the branch head")
    head = await git("rev-parse", "HEAD")
    base = await git(
        "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}"
    )
    merge_base = await git("merge-base", base, head)
    ref = await git(
        "rev-parse", "--verify", "--symbolic-full-name", "--end-of-options", args.base
    )
    ref = ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    if not ref or args.base == "HEAD" or ref == "main":
        ref = DEFAULT_BASE
    paths = await git("diff", "--name-only", "-z", "--no-renames", merge_base, head)
    checks = (
        (args.check,)
        if args.check
        else checks_for(
            paths.split("\0")[:-1], has_draft=await asyncio.to_thread(DRAFT.is_file)
        )
    )
    revisions = Revisions(base, merge_base, head, ref)
    results = []
    with TemporaryDirectory(prefix="preflight-") as scratch:
        for name in checks:
            sys.stderr.write(f"Running {name}\n")
            try:
                await check(name, revisions, Path(scratch))
            except (OSError, CheckError) as error:
                message = str(error) or f"{name} timed out"
                sys.stderr.write(f"{message}\n")
                results.append((name, False))
            else:
                results.append((name, True))
    sys.stdout.write(summary(results, args.base) + "\n")
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(asyncio.run(main()))
