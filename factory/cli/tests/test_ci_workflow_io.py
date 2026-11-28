"""Exercise the root CI workflow and its prose hooks."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKSPACE = next(
    parent.parent
    for parent in Path(__file__).resolve().parents
    if parent.name == "factory"
)
SHA256SUM = f"""sha256sum() {{
    {shlex.quote(sys.executable)} -I -S -c '
import hashlib
import sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest(), " -")
'
}}
"""


def _mapping(value: object) -> dict[object, object]:
    assert isinstance(value, dict)
    return value


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _workflow_jobs() -> dict[object, object]:
    workflow_path = WORKSPACE / ".github/workflows/ci.yml"
    workflow = _mapping(yaml.safe_load(workflow_path.read_text()))
    return _mapping(workflow["jobs"])


def _workflow_steps(job: object) -> list[dict[object, object]]:
    steps = _list(_mapping(job)["steps"])
    return [_mapping(step) for step in steps]


def _shell_command(step: dict[object, object], prefix: str = "") -> list[str]:
    shell = step["shell"]
    script = step["run"]
    assert isinstance(shell, str)
    assert isinstance(script, str)
    return [shell, "-eo", "pipefail", "-c", prefix + script]


def test_ci_shards_affected_targets(tmp_path: Path) -> None:
    jobs = _workflow_jobs()
    affected = _mapping(jobs["affected-targets"])
    bazel = _mapping(jobs["bazel"])
    strategy = _mapping(bazel["strategy"])
    shards = _list(_mapping(strategy["matrix"])["shard"])
    assert shards == [1, 2, 3, 4]
    assert strategy["fail-fast"] is False
    assert bazel["needs"] == "affected-targets"
    assert "outputs" not in bazel
    assert {"affected-targets", "bazel"}.issubset(
        _list(_mapping(jobs["conclusion"])["needs"])
    )

    checkout, setup, cache, selector, publish, upload = _workflow_steps(affected)
    assert affected["environment"] == bazel["environment"]
    assert affected["runs-on"] == bazel["runs-on"]
    assert _mapping(checkout["with"])["fetch-depth"] == 0
    assert setup["uses"] == "./.github/actions/setup-bazel-ci"
    assert selector["uses"] == "./.github/actions/affected-targets"
    assert selector["with"] == {"base": "${{ inputs.base }}"}
    assert publish["id"] == "selection"
    assert affected["outputs"] == {
        "count": "${{ steps.affected.outputs.count }}",
        "digest": "${{ steps.selection.outputs.digest }}",
    }
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert upload["with"] == {
        "name": "affected-targets",
        "path": "${{ runner.temp }}/affected-targets.txt",
        "retention-days": 3,
        "if-no-files-found": "error",
    }

    steps = _workflow_steps(bazel)
    assert cache in steps
    assert all(step.get("uses") != selector["uses"] for step in steps)
    download = next(
        step for step in steps if step["name"] == "Download the affected targets"
    )
    assert str(download["uses"]).startswith("actions/download-artifact@")
    assert download["with"] == {
        "name": "affected-targets",
        "path": "${{ runner.temp }}",
    }
    assert "if" not in download
    test = next(step for step in steps if step["name"] == "Test")
    assert steps.index(download) < steps.index(test)
    assert test["if"] == "needs.affected-targets.outputs.count != '0'"
    assert test["env"] == {
        "TARGETS": "${{ runner.temp }}/affected-targets.txt",
        "SHARD": "${{ matrix.shard }}",
        "SHARD_COUNT": "${{ strategy.job-total }}",
    }

    targets = tmp_path / "affected targets.txt"
    fake_bazel = """bazel() {
        [[ "$*" == "test --config=ci --build_tests_only --target_pattern_file="* ]]
        cat "${4#--target_pattern_file=}"
    }
    """
    no_exclusions: list[str] = []
    empty_case: tuple[list[str], list[str]] = ([], [])
    for selected, exclusions in (
        (["//:d", "//:a", "//:e", "//:b", "//:c"], no_exclusions),
        (["@rules//...", "//..."], ["-//:manual", "-@rules//:manual"]),
        empty_case,
    ):
        targets.write_text("\n".join([*selected, *exclusions]))
        for shard in shards:
            assert isinstance(shard, int)
            run = subprocess.run(
                _shell_command(test, fake_bazel),
                env={
                    **os.environ,
                    "TARGETS": str(targets),
                    "RUNNER_TEMP": str(tmp_path),
                    "SHARD": str(shard),
                    "SHARD_COUNT": str(len(shards)),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            expected = sorted(selected)[shard - 1 :: len(shards)]
            expected_output = (
                [*expected, *exclusions]
                if expected
                else [f"No targets in shard {shard}."]
            )
            assert run.stdout.splitlines() == expected_output


AGENT_CAPACITY_REF = "refs/heads/gh-readonly-queue/main/pr-209-0123abcd"
AGENT_CAPACITY_UPLOAD_CONDITION = "steps.merge-group-ref.outputs.ref_gone != 'true'"


def _agent_capacity_steps() -> tuple[dict[object, object], dict[object, object]]:
    steps = _workflow_steps(_workflow_jobs()["agent-capacity"])
    check = next(step for step in steps if step["name"] == "Check the merge group ref")
    upload = next(step for step in steps if step["name"] == "Upload the Go module scan")
    return check, upload


def _upload_runs(ref_gone: str) -> bool:
    """Evaluate the upload condition for the check step's `ref_gone` output."""
    match = re.fullmatch(
        r"steps\.merge-group-ref\.outputs\.ref_gone != '(\w+)'",
        AGENT_CAPACITY_UPLOAD_CONDITION,
    )
    assert match
    return ref_gone != match.group(1)


def _run_merge_group_ref_check(
    tmp_path: Path, stub_body: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    check, _ = _agent_capacity_steps()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh calls.txt"
    stub = bin_dir / "gh"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        + stub_body
    )
    stub.chmod(0o755)
    output = tmp_path / "github output.txt"
    output.touch()
    # The job's steps set no shell; the helper needs one to build the command.
    run = subprocess.run(
        _shell_command({**check, "shell": "bash"}),
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "example/repo",
            "GITHUB_REF": AGENT_CAPACITY_REF,
            "GH_TOKEN": "stub-token",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return run, output, calls


def test_agent_capacity_upload_skips_only_on_gone_ref() -> None:
    check, upload = _agent_capacity_steps()
    assert "continue-on-error" not in upload
    assert check["id"] == "merge-group-ref"
    assert check["if"] == "github.event_name == 'merge_group'"
    assert check["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "continue-on-error" not in check
    # The condition is asserted as an exact string, as for the coverage steps.
    assert upload["if"] == AGENT_CAPACITY_UPLOAD_CONDITION
    uses = upload["uses"]
    assert isinstance(uses, str)
    assert uses.startswith("github/codeql-action/upload-sarif@")
    assert upload["with"] == {
        "sarif_file": "${{ runner.temp }}/gomodscan.sarif",
        "category": "agent-capacity-dependencies",
    }


def test_agent_capacity_ref_gone_skips_upload(tmp_path: Path) -> None:
    run, output, calls = _run_merge_group_ref_check(
        tmp_path, 'echo "gh: Not Found (HTTP 404)" >&2\nexit 1\n'
    )
    assert run.returncode == 0, run.stderr
    assert output.read_text().splitlines() == ["ref_gone=true"]
    assert (
        "::notice::merge group ref is gone, the queue dissolved the group; "
        "skipping the Go module scan upload"
    ) in run.stdout.splitlines()
    assert not _upload_runs("true")
    assert calls.read_text().split()[0] == "api"


def test_agent_capacity_live_ref_uploads(tmp_path: Path) -> None:
    run, output, _ = _run_merge_group_ref_check(tmp_path, "echo '{}'\n")
    assert run.returncode == 0, run.stderr
    assert output.read_text().splitlines() == ["ref_gone=false"]
    assert "::notice::" not in run.stdout
    assert _upload_runs("false")
    # A skipped check step (pull_request, workflow_dispatch) has an empty output.
    assert _upload_runs("")


@pytest.mark.parametrize("status", ["500", "403"])
def test_agent_capacity_unanswered_read_fails_job(tmp_path: Path, status: str) -> None:
    run, output, _ = _run_merge_group_ref_check(
        tmp_path, f'echo "gh: Server said no (HTTP {status})" >&2\nexit 1\n'
    )
    assert run.returncode != 0
    assert "ref_gone=true" not in output.read_text()
    assert "::notice::" not in run.stdout


def test_agent_capacity_ref_call_uses_the_queue_ref(tmp_path: Path) -> None:
    _, _, calls = _run_merge_group_ref_check(tmp_path, "exit 0\n")
    api_call = calls.read_text().split()
    assert api_call[0] == "api"
    assert (
        "repos/example/repo/git/ref/heads/gh-readonly-queue/main/pr-209-0123abcd"
        in api_call
    )


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (b"//:b\n//:a\n", b"//:a\n//:b\n"),
        (b"", b""),
        (b"//...\n-//:manual\n", b"-//:manual\n//...\n"),
    ],
)
def test_ci_publishes_affected_targets(
    tmp_path: Path, targets: bytes, expected: bytes
) -> None:
    steps = _workflow_steps(_workflow_jobs()["affected-targets"])
    publish = next(step for step in steps if step.get("id") == "selection")
    source = tmp_path / "targets.txt"
    source.write_bytes(targets)
    log = source.with_name("target-determinator.log")
    log.write_text("//:a Changes: changed source\n")
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    sorted_targets = tmp_path / "affected-targets.txt"
    run = subprocess.run(
        _shell_command(publish, SHA256SUM),
        env={
            **os.environ,
            "TARGETS": str(source),
            "SORTED_TARGETS": str(sorted_targets),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert sorted_targets.read_bytes() == expected
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "digest": hashlib.sha256(expected).hexdigest(),
    }
    labels = expected.decode().splitlines()
    for printed in (run.stdout, summary.read_text()):
        lines = [line.removeprefix("  | ").strip() for line in printed.splitlines()]
        assert [line for line in lines if line in labels] == labels
        if not labels:
            assert "No affected targets." in lines
    assert log.read_text().strip() in run.stdout


def test_ci_test_steps_do_not_exclude_slow_external() -> None:
    jobs = _workflow_jobs()
    test_runs: list[str] = []
    for job in jobs.values():
        for step in _workflow_steps(job):
            script = step.get("run")
            if isinstance(script, str) and re.search(
                r"\bbazel(?:isk)?\s+test\b", script
            ):
                test_runs.append(script)
    assert test_runs
    for script in test_runs:
        assert "--test_tag_filters" not in script
        assert "slow-external" not in script


def test_ci_runs_cli_coverage_in_parallel() -> None:
    jobs = _workflow_jobs()
    bazel = _mapping(jobs["bazel"])
    coverage = _mapping(jobs["cli-coverage"])
    assert "needs" not in coverage
    assert coverage["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'merge_group'"
    )
    assert coverage["runs-on"] == bazel["runs-on"]
    assert coverage["environment"] == (
        "${{ github.event_name == 'merge_group' && 'cache' || '' }}"
    )

    checkout, setup, cache, selector, check = _workflow_steps(coverage)
    bazel_checkout, bazel_setup, bazel_cache, *_ = _workflow_steps(bazel)
    assert checkout == bazel_checkout
    assert setup == {
        **bazel_setup,
        "with": {
            **_mapping(bazel_setup["with"]),
            "disk-cache": "${{ github.workflow }}-${{ github.job }}-ubuntu-26.04",
        },
    }
    assert cache == bazel_cache
    assert selector["uses"] == "./.github/actions/affected-targets"
    assert selector["with"] == {"base": "${{ inputs.base }}"}
    assert check["if"] == "steps.affected.outputs.count != '0'"
    assert check["run"] == "bazelisk run --config=ci //factory/cli:coverage"
    assert check["id"] == "coverage"
    assert coverage["outputs"] == {"coverage": "${{ steps.coverage.outcome }}"}


@pytest.mark.parametrize(
    ("event", "targets", "job_result", "step_outcome", "accepted"),
    [
        ("pull_request", b"//:tests\n", "success", "success", True),
        ("merge_group", b"//:tests\n", "success", "success", True),
        ("pull_request", b"", "success", "skipped", True),
        ("merge_group", b"//:tests\n", "success", "skipped", False),
        ("pull_request", b"//:tests\n", "failure", "failure", False),
        ("merge_group", b"//:tests\n", "cancelled", "cancelled", False),
        ("merge_group", b"", "skipped", "", False),
        ("workflow_dispatch", b"//:tests\n", "skipped", "", True),
        ("pull_request", None, "success", "success", False),
    ],
)
def test_ci_conclusion_requires_cli_coverage(
    event: str,
    targets: bytes | None,
    job_result: str,
    step_outcome: str,
    accepted: bool,
) -> None:
    conclusion = _mapping(_workflow_jobs()["conclusion"])
    required = _list(conclusion["needs"])
    assert "cli-coverage" in required
    needs: dict[object, dict[str, object]] = {
        job: {"result": "success"} for job in required
    }
    if event != "pull_request":
        needs["pr-size"]["result"] = "skipped"
    digest = hashlib.sha256(targets).hexdigest() if targets is not None else ""
    needs["affected-targets"]["outputs"] = {"digest": digest}
    needs["cli-coverage"] = {
        "result": job_result,
        "outputs": {"coverage": step_outcome},
    }

    step = next(step for step in _workflow_steps(conclusion) if "run" in step)
    run = subprocess.run(
        _shell_command(step, SHA256SUM),
        env={**os.environ, "EVENT": event, "NEEDS": json.dumps(needs)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert (run.returncode == 0) == accepted, run.stderr


@pytest.mark.parametrize("job", ["affected-targets", "bazel"])
def test_ci_conclusion_rejects_failed_selection_or_shard(job: str) -> None:
    conclusion = _workflow_jobs()["conclusion"]
    needs: dict[object, dict[str, object]] = {
        name: {"result": "success"} for name in _list(_mapping(conclusion)["needs"])
    }
    needs["affected-targets"]["outputs"] = {
        "digest": hashlib.sha256(b"//:tests\n").hexdigest()
    }
    needs["cli-coverage"]["outputs"] = {"coverage": "success"}
    needs[job]["result"] = "failure"
    step = next(step for step in _workflow_steps(conclusion) if "run" in step)
    run = subprocess.run(
        _shell_command(step, SHA256SUM),
        env={**os.environ, "EVENT": "pull_request", "NEEDS": json.dumps(needs)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert run.stderr.strip() == "A required job did not succeed."


@pytest.fixture
def prose_repo(tmp_path: Path) -> tuple[str, dict[str, str]]:
    imported_copy = "imported [guide].md"
    (tmp_path / imported_copy).write_text("A prose finding.\n")
    (tmp_path / "clean.md").write_text("Clean prose.\n")
    skip_list = tmp_path / ".github/prose-lint-debt.txt"
    skip_list.parent.mkdir()
    script = Path(".github/prose-lint.sh")
    (tmp_path / script).write_bytes((WORKSPACE / script).read_bytes())
    (tmp_path / ".vale.ini").touch()
    (tmp_path / ".vale/project").mkdir(parents=True)
    (tmp_path / ".vale/config/vocabularies").mkdir(parents=True)
    skip_list.write_text(f"# Example import\n\n{imported_copy}\n")

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)

    fake_linter = r"""#!/bin/bash
for path in "$@"; do
    [[ $path == *.md ]] || continue
    printf '%s: %s\n' "${0##*/}" "$path"
    if grep -qF 'A prose finding.' "$path"; then
        exit 1
    fi
done
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["vale", "cspell", "rumdl"]:
        tool = bin_dir / name
        tool.write_text(fake_linter)
        tool.chmod(0o755)

    mise = bin_dir / "mise"
    mise.write_text(
        '#!/bin/bash\n[[ $1 == exec && $2 == -- ]] || exit 2\nshift 2\nexec "$@"\n'
    )
    mise.chmod(0o755)
    return imported_copy, {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.parametrize("case", ["listed", "unlisted", "missing"])
def test_ci_prose_skip_list(
    tmp_path: Path, prose_repo: tuple[str, dict[str, str]], case: str
) -> None:
    steps = _workflow_steps(_workflow_jobs()["prose"])
    lint = next(step for step in steps if step["name"] == "Lint prose")
    imported_copy, env = prose_repo
    skip_list = tmp_path / ".github/prose-lint-debt.txt"
    if case == "unlisted":
        skip_list.write_text("")
    if case == "missing":
        skip_list.write_text(f"{imported_copy}\nremoved.md\n")

    run = subprocess.run(
        _shell_command(lint),
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    if case == "listed":
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines() == [
            "vale: clean.md",
            "cspell: clean.md",
            "rumdl: clean.md",
        ]
    elif case == "unlisted":
        assert run.returncode != 0
        assert f"vale: {imported_copy}" in run.stdout.splitlines()
    else:
        assert run.returncode != 0
        assert (
            "Prose skip list entry must name an existing Markdown file: removed.md"
            in run.stderr
        )


def _pre_commit_config() -> dict[object, object]:
    path = WORKSPACE / ".pre-commit-config.yaml"
    config = _mapping(yaml.safe_load(path.read_text()))
    assert config.get("files", "") == ""
    return config


@pytest.mark.parametrize(
    "path",
    [
        "lib/rules/e2e/smoke/testdata/proofs/cspell_bad/notes.md",
        "lib/rules/e2e/smoke/testdata/proofs/rumdl_bad/bad.md",
    ],
)
def test_prose_hooks_exclude_smoke_fixtures(path: str) -> None:
    exclude = _pre_commit_config()["exclude"]
    assert isinstance(exclude, str)
    assert re.search(exclude, path)


def _prose_hook(name: str) -> dict[object, object]:
    for repo in _list(_pre_commit_config()["repos"]):
        for entry in _list(_mapping(repo)["hooks"]):
            hook = _mapping(entry)
            if hook["id"] == name:
                return hook
    pytest.fail(f"Missing {name} hook")


@pytest.mark.parametrize("name", ["cspell", "rumdl"])
def test_prose_hook_checks_listed_markdown(
    tmp_path: Path, prose_repo: tuple[str, dict[str, str]], name: str
) -> None:
    imported_copy, env = prose_repo
    hook = _prose_hook(name)
    assert (
        hook.get("files", ""),
        hook.get("exclude", "^$"),
        hook.get("types"),
        hook.get("types_or", []),
        hook.get("exclude_types", []),
        hook.get("pass_filenames", True),
    ) == ("", "^$", ["markdown"], [], [], True)
    entry = hook["entry"]
    assert isinstance(entry, str)
    assert hook.get("args", []) == []
    run = subprocess.run(
        [*shlex.split(entry), imported_copy],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.stdout.splitlines() == [f"{name}: {imported_copy}"]
    assert run.returncode == 1


def test_rumdl_hook_rejects_untracked_debt_without_files(
    tmp_path: Path, prose_repo: tuple[str, dict[str, str]]
) -> None:
    imported_copy, env = prose_repo
    hook = _prose_hook("rumdl")
    assert hook["always_run"] is True
    entry = hook["entry"]
    assert isinstance(entry, str)
    subprocess.run(
        ["git", "rm", "--cached", "--", imported_copy], cwd=tmp_path, check=True
    )
    run = subprocess.run(
        shlex.split(entry),
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert imported_copy in run.stderr
