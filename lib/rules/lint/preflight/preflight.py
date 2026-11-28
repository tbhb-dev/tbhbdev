# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import argparse
import asyncio
import errno
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

DEFAULT_BASE = "origin/main"
SELECTION = "selection"
CLI_COVERAGE_INPUTS = {"factory/cli/BUILD.bazel", "factory/cli/combine_coverage.sh"}
# A selected label under the CLI package; select.sh may spell the main
# repository with a leading @@.
CLI_TARGET = re.compile(r"(@@)?//factory/cli([:/]|$)")
clock = time.monotonic
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


# True: the stage passed. False: it failed. None: the selection proved it unaffected.
Outcome = bool | None


def summary(results: list[tuple[str, Outcome]], base: str) -> str:
    lines = []
    for name, passed in results:
        status = "SKIP" if passed is None else "PASS" if passed else "FAIL"
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
    if not (hasattr(os, "killpg") and hasattr(signal, "SIGKILL")):
        message = "Preflight requires POSIX process groups."
        raise RuntimeError(message)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE if capture else None,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(stdin), seconds)
    except TimeoutError, asyncio.CancelledError:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
        raise
    if process.returncode:
        message = f"{shlex.join(map(str, command))} exited {process.returncode}"
        raise CheckError(message)
    return output or b""


async def git(*args: str) -> str:
    return os.fsdecode(await run("git", *args, capture=True)).rstrip("\n")


def write_record(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)


async def record_result(
    revisions: Revisions, results: list[tuple[str, Outcome]]
) -> None:
    head, base = revisions.head, revisions.merge_base
    if base == head or await git("rev-parse", "HEAD") != head:
        return
    if await git("status", "--porcelain", "--untracked-files=no"):
        return
    records = Path(await git("rev-parse", "--git-common-dir")) / "tbhbdev-preflight"
    tree = await git("rev-parse", f"{head}^{{tree}}")
    failed = " ".join(name for name, passed in results if passed is False)
    result = f"fail {failed}" if failed else "pass"
    await asyncio.to_thread(
        write_record, records / head, f"tree {tree}\nbase {base}\nresult {result}\n"
    )


def selected_file(selection: str) -> str:
    lines = selection.splitlines()
    return next((line[5:] for line in lines if line.startswith("file=")), "")


async def cli_tests_affected(scratch: Path, paths: list[str]) -> bool:
    """Whether changed paths or the test selection require CLI coverage."""
    if CLI_COVERAGE_INPUTS.intersection(paths):
        return True
    try:
        selection = await asyncio.to_thread((scratch / SELECTION).read_text)
        # A fallback selection lists patterns such as //... and not labels.
        target_file = selected_file(selection)
        if "all=false" not in selection.splitlines() or not target_file:
            return True
        targets = await asyncio.to_thread(Path(target_file).read_text)
    except OSError, ValueError:
        return True
    return any(CLI_TARGET.match(line) for line in targets.splitlines())


async def affected_tests(base: str, head: str, scratch: Path) -> None:
    determinator = scratch / "target-determinator"
    output = scratch / SELECTION
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
    target_file = selected_file(selection)
    if not target_file:
        raise OSError(errno.EIO, "affected-targets did not report a target file")
    if (await asyncio.to_thread(Path(target_file).stat)).st_size:
        await run(
            *BAZEL_TEST,
            "--test_tag_filters=-slow-external",
            "--build_tests_only",
            f"--target_pattern_file={target_file}",
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
            target = "@tbhb_rules//lint:lint_baselines"
            await run(*BAZEL_RUN, target, "--", merge_base, head)
        case "gitleaks-text":
            draft = await asyncio.to_thread(DRAFT.read_bytes)
            await run(*BAZEL_RUN, "@tbhb_rules//lint:gitleaks_text", stdin=draft)
        case "module-tidy" | "prose":
            prefix = ("mise", "exec", "--") if name == "prose" else ()
            await run(*prefix, "bash", script, name, scratch)
        case "agent-capacity-vulnerabilities":
            target = "//factory/agent-capacity:govulncheck"
            report = scratch / "agent-capacity.sarif"
            await run(*BAZEL_RUN, target, "--", f"--sarif={report}")
        case "agent-capacity-modules":
            scanner, vendor = scratch / "gomodscan", scratch / "vendored"
            await run("bash", script, "scanner-checkout", scanner, seconds=120)
            await run("bash", script, "vendor", vendor)
            report = scratch / "gomodscan.sarif"
            await run("bash", script, "modules", scanner, vendor, report)
        case "pr-size" | "design-report":
            checkout = scratch / "base"
            await run("git", "worktree", "add", "--quiet", "--detach", checkout, base)
            try:
                if name == "pr-size":
                    branch = await git("branch", "--show-current")
                    args = (base, head, ref, DEFAULT_BASE, branch)
                    await run("bash", script, name, *args, cwd=checkout)
                    return
                env = {**os.environ, "PYTHON": sys.executable}
                await run("bash", script, name, checkout, scratch, env=env)
            finally:
                await run("git", "worktree", "remove", "--force", checkout)
        case _:
            raise OSError(errno.EINVAL, "unknown check", name)


def say(text: str) -> None:
    sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


async def stage(
    name: str,
    revisions: Revisions,
    scratch: Path,
    explicit: str | None,
    paths: list[str],
) -> Outcome:
    if name == "cli-coverage" and not (
        explicit or await cli_tests_affected(scratch, paths)
    ):
        say(f"Skipping {name}: no CLI test affected")
        return None
    say(f"Running {name}")
    began = clock()
    passed = True
    try:
        await check(name, revisions, scratch)
    except (OSError, CheckError) as error:
        say(str(error) or f"{name} timed out")
        passed = False
    say(f"Finished {name} in {clock() - began:.0f} s ({'PASS' if passed else 'FAIL'})")
    return passed


async def main() -> int:
    started = clock()
    parser = argparse.ArgumentParser(
        description="Run the CI checks at the current commit."
    )
    parser.add_argument("base", nargs="?", default=DEFAULT_BASE)
    parser.add_argument("--check", choices=checks_for(["BUILD.bazel"], has_draft=True))
    args = parser.parse_args()
    os.chdir(os.environ.get("BUILD_WORKSPACE_DIRECTORY", "."))
    # Keep the Bazel Python launcher's import policy scoped to this process.
    os.environ.pop("PYTHONSAFEPATH", None)
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
    ref = DEFAULT_BASE if args.base == "HEAD" or ref in {"", "main"} else ref
    changed = await git("diff", "--name-only", "-z", "--no-renames", merge_base, head)
    paths = changed.split("\0")[:-1]
    if args.check:
        checks = (args.check,)
    else:
        has_draft = await asyncio.to_thread(DRAFT.is_file)
        checks = checks_for(paths, has_draft=has_draft)
    revisions = Revisions(base, merge_base, head, ref)
    scratch = Path(tempfile.mkdtemp(prefix="preflight-"))
    # One stage at a time: each await finishes before the next name starts.
    results = [
        (n, await stage(n, revisions, scratch, args.check, paths)) for n in checks
    ]
    sys.stdout.write(summary(results, args.base) + "\n")
    if not args.check:
        await record_result(revisions, results)
    passed = False not in (outcome for _, outcome in results)
    word = "pass" if passed else "fail"
    say(f"Preflight finished in {clock() - started:.0f} s: {word}")
    reports = {"design-report.md", "gomodscan.sarif", "agent-capacity.sarif"}
    if not passed or reports.intersection(await asyncio.to_thread(os.listdir, scratch)):
        sys.stdout.write(f"Preflight files: {scratch}\n")
    else:
        await asyncio.to_thread(shutil.rmtree, scratch)
    return 0 if passed else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(asyncio.run(main()))
