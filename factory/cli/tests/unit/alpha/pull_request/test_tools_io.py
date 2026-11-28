# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request tools."""

import os
import re
import sys
from pathlib import Path

import pytest
from fake_tool import (
    write_fake,
)
from unit.alpha.pull_request import pr_test_support
from unit.alpha.pull_request.pr_test_support import (
    TOOL_VERSIONS,
    World,
    run_main,
    write_draft,
)

from tbhbdev.alpha import tool_pins
from tbhbdev.alpha.pull_request import tools as pr_tools

draft_world = pr_test_support.draft_world
host_names = pr_test_support.host_names
world = pr_test_support.world


@pytest.mark.parametrize("pin", [None, '{"vale": ""}', '{"vale": 123}', "{}", "{"])
def test_tool_version_pin_must_be_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin: str | None
) -> None:
    path = tmp_path / "pins.json"
    monkeypatch.setattr(tool_pins, "VERSIONS", path)
    if pin is not None:
        path.write_text(pin)
    with pytest.raises(SystemExit, match=r"vale version pin.*pins.json"):
        tool_pins.pinned_version("vale")


def worktree_tool(
    world: World,
    name: str,
    body: str = "raise SystemExit('unexpected shim invocation')",
) -> Path:
    shim = world.root / ".bazel_env" / "bin" / name
    shim.parent.mkdir(parents=True, exist_ok=True)
    write_fake(shim, body)
    return shim


def test_find_tool_prefers_the_pinned_shim_to_path(draft_world: World) -> None:
    shim = worktree_tool(draft_world, "vale", f"print({TOOL_VERSIONS['vale']!r})")
    write_fake(draft_world.bin / "vale", "print('0.0.0')")
    assert pr_tools.find_tool("vale") == str(shim)


def test_find_tool_prefers_the_raw_gitleaks_shim(draft_world: World) -> None:
    worktree_tool(draft_world, "gitleaks")
    shim = worktree_tool(
        draft_world, "gitleaks-raw", f"print({TOOL_VERSIONS['gitleaks']!r})"
    )
    assert pr_tools.find_tool("gitleaks", unwrapped=True) == str(shim)


def test_find_tool_accepts_an_older_raw_gitleaks_shim(draft_world: World) -> None:
    worktree_tool(draft_world, "gitleaks")
    shim = worktree_tool(draft_world, "gitleaks-raw", "print('8.29.0')")
    assert pr_tools.find_tool("gitleaks", unwrapped=True) == str(shim)


def test_find_tool_refuses_a_broken_raw_gitleaks_shim(draft_world: World) -> None:
    worktree_tool(draft_world, "gitleaks")
    shim = worktree_tool(draft_world, "gitleaks-raw", "import sys\nsys.exit(1)")
    with pytest.raises(SystemExit) as error:
        pr_tools.find_tool("gitleaks", unwrapped=True)
    assert str(error.value) == f"gitleaks at {shim} reports no version"


def test_find_tool_refuses_the_wrong_version(draft_world: World) -> None:
    version = TOOL_VERSIONS["vale"]
    write_fake(draft_world.bin / "vale", f"print('vale version {version}0')")
    with pytest.raises(
        SystemExit, match=rf"vale.*{re.escape(version)}0.*{re.escape(version)}"
    ):
        pr_tools.find_tool("vale")


def test_find_tool_refuses_a_wrong_mise_version(
    draft_world: World, tmp_path: Path
) -> None:
    (draft_world.bin / "cspell").unlink()
    installed = tmp_path / "mise-cspell"
    write_fake(installed, "print('10.5.1')")
    draft_world.fake("mise", stdout=f"{installed}\n")
    version = TOOL_VERSIONS["cspell"]
    with pytest.raises(SystemExit) as error:
        pr_tools.find_tool("cspell")
    assert str(error.value) == (
        f"cspell at {installed} reports 10.5.1, expected pinned version {version}"
    )


@pytest.mark.parametrize(
    ("name", "reported"),
    [
        ("cspell", "10.5.1"),
        ("gitleaks", "8.29.0"),
        ("rumdl", "rumdl 0.3.4"),
        ("scc", "scc version 4.0.0"),
        ("vale", "vale version 3.27.0"),
    ],
)
def test_find_tool_accepts_an_older_shim_even_with_the_right_path_version(
    draft_world: World, name: str, reported: str
) -> None:
    shim = worktree_tool(draft_world, name, f"print({reported!r})")
    assert pr_tools.find_tool(name) == str(shim)


@pytest.mark.parametrize(
    "body",
    [
        "pass",
        "print('missing version info')",
        "print('not-a-version')",
        "print('4.1.0'); sys.exit(2)",
    ],
)
def test_find_tool_requires_a_successful_version_report(
    draft_world: World, body: str
) -> None:
    shim = worktree_tool(draft_world, "scc", f"import sys\n{body}")
    with pytest.raises(SystemExit) as error:
        pr_tools.find_tool("scc")
    assert str(error.value) == f"scc at {shim} reports no version"


def test_find_tool_refuses_an_unstartable_shim(draft_world: World) -> None:
    shim = draft_world.root / ".bazel_env" / "bin" / "cspell"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/nonexistent/interpreter\n")
    shim.chmod(0o755)
    with pytest.raises(
        SystemExit,
        match=rf"^cannot check cspell version at {re.escape(str(shim))}: ",
    ):
        pr_tools.find_tool("cspell")


def test_find_tool_refuses_an_unusable_mise_tool(draft_world: World) -> None:
    (draft_world.bin / "vale").unlink()
    draft_world.fake("mise", stdout="/missing/vale\n")
    with pytest.raises(SystemExit, match=r"cannot check vale version at /missing/vale"):
        pr_tools.find_tool("vale")


def test_find_tool_prefers_the_worktree_shim_to_mise(
    draft_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = worktree_tool(draft_world, "cog")
    (draft_world.bin / "cog").unlink()
    draft_world.fake("mise", stdout="/mise/cog\n")
    monkeypatch.chdir(draft_world.root / ".github")
    assert pr_tools.find_tool("cog") == str(shim)
    assert draft_world.calls("mise") == []


def test_find_tool_can_skip_the_bazel_shims(
    draft_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shim = worktree_tool(draft_world, "vale", f"print({TOOL_VERSIONS['vale']!r})")
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{path}")
    assert pr_tools.find_tool("vale") == str(shim)
    assert pr_tools.find_tool("vale", unwrapped=True) == str(draft_world.bin / "vale")
    (draft_world.bin / "vale").unlink()
    installed = tmp_path / "mise-vale"
    write_fake(installed, f"print('vale version ' + {TOOL_VERSIONS['vale']!r})")
    draft_world.fake("mise", stdout=f"{installed}\n")
    monkeypatch.setenv("PATH", path)
    assert pr_tools.find_tool("vale") == str(shim)
    assert pr_tools.find_tool("vale", unwrapped=True) == str(installed)


def test_find_tool_falls_back_to_mise(draft_world: World, tmp_path: Path) -> None:
    shim = worktree_tool(draft_world, "absent_tool")
    shim.chmod(0o644)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "absent_tool").write_text("")
    draft_world.fake("mise", stdout=f"{elsewhere / 'absent_tool'}\n")
    assert pr_tools.find_tool("absent_tool") == str(elsewhere / "absent_tool")
    assert draft_world.calls("mise")[0]["argv"] == ["which", "absent_tool"]
    shim.chmod(0o755)
    assert pr_tools.find_tool("absent_tool") == str(shim)


def test_find_tool_missing(draft_world: World) -> None:
    with pytest.raises(SystemExit, match=r"absent_tool not found in \.bazel_env"):
        pr_tools.find_tool("absent_tool")
    draft_world.fake("mise", rc=1, stderr="not installed")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr_tools.find_tool("absent_tool")
    draft_world.fake("mise", rc=0, stdout="\n")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr_tools.find_tool("absent_tool")


def test_find_tools_returns_each_path(world: World) -> None:
    shim = worktree_tool(world, "cog")
    (world.bin / "cog").unlink()
    leak = worktree_tool(world, "gitleaks-raw", f"print({TOOL_VERSIONS['gitleaks']!r})")
    (world.bin / "gitleaks").unlink()
    assert pr_tools.find_tools(["cog", "vale", "gitleaks"], unwrapped={"gitleaks"}) == {
        "cog": str(shim),
        "vale": str(world.bin / "vale"),
        "gitleaks": str(leak),
    }


@pytest.mark.usefixtures("world")
def test_find_tools_reports_all_missing_names() -> None:
    with pytest.raises(SystemExit) as error:
        pr_tools.find_tools(["first", "vale", "second"])
    assert str(error.value) == (
        "missing tools: first, second; "
        "run `bazelisk run //:bazel_env` or `mise install`"
    )


@pytest.mark.parametrize(
    ("verb", "extra"), [("check", ()), ("create", ("gitleaks", "gh"))]
)
def test_pr_reports_all_missing_tools_before_reading_the_draft(
    world: World, verb: str, extra: tuple[str, ...]
) -> None:
    missing = ("cog", "cspell", *extra)
    for name in missing:
        (world.bin / name).unlink()
    worktree_tool(world, "gitleaks")
    with pytest.raises(SystemExit) as error:
        run_main(verb)
    assert str(error.value) == (
        f"missing tools: {', '.join(missing)}; "
        "run `bazelisk run //:bazel_env` or `mise install`"
    )


@pytest.mark.parametrize("verb", ["check", "create"])
def test_pr_refuses_to_run_without_scc(world: World, verb: str) -> None:
    (world.bin / "scc").unlink()
    write_draft(world)
    args = ("create", "--dry-run") if verb == "create" else ("check",)
    with pytest.raises(SystemExit, match="missing tools: scc"):
        run_main(*args)


def test_create_reuses_the_tools_resolved_at_startup(world: World) -> None:
    gh = world.bin / "gh"
    installed_gh = gh.with_name("installed-gh")
    gh.rename(installed_gh)
    git = world.root.parent / "git_dir" / "git"
    installed_git = world.root.parent / "installed" / git.name
    installed_git.parent.mkdir()
    git.rename(installed_git)
    world.fake(
        "mise",
        cases={
            "which gh": {"rc": 0, "stdout": f"{installed_gh}\n", "stderr": ""},
            "which git": {"rc": 0, "stdout": f"{installed_git}\n", "stderr": ""},
        },
    )
    write_draft(world)
    assert run_main("create", "--dry-run") == 0
    assert [call["argv"] for call in world.calls("mise")] == [
        ["which", "git"],
        ["which", "gh"],
    ]
    installed_gh.rename(gh)
    assert pr_tools.find_tool("gh") == str(gh)


def test_create_needs_gh(world: World) -> None:
    (world.bin / "gh").unlink()
    write_draft(world)
    with pytest.raises(SystemExit) as error:
        run_main("create")
    assert str(error.value) == (
        "missing tools: gh; run `bazelisk run //:bazel_env` or `mise install`"
    )


def test_run_gives_the_child_no_terminal(world: World) -> None:
    del world
    done = pr_tools.run(
        [
            str(Path(sys.executable)),
            "-c",
            "import sys;print(sys.stdin.isatty(), repr(sys.stdin.read()))",
        ]
    )
    assert done.stdout == "False ''\n"
    done = pr_tools.run(
        [sys.executable, "-c", "import sys;print(sys.stdin.read())"], stdin="hi"
    )
    assert done.stdout == "hi\n"


def test_run_uses_the_given_directory(tmp_path: Path) -> None:
    done = pr_tools.run(
        [sys.executable, "-c", "import os;print(os.getcwd())"], cwd=tmp_path
    )
    assert done.stdout.strip() == str(tmp_path.resolve())


@pytest.fixture
def hooked(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root)
    return world


def test_root_outside_a_repository_is_the_directory_given(
    hooked: World, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert Path.cwd() == hooked.root
    assert pr_tools.root(elsewhere) == elsewhere


def test_run_refreshes_gh_token_before_invoking_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = tmp_path / "gh"
    write_fake(gh, "print('ok')")
    events: list[str] = []
    monkeypatch.setattr(
        pr_tools.gh_refresh, "refresh_if_served", lambda: events.append("refresh")
    )
    assert pr_tools.run([str(gh), "pr", "create"]).stdout == "ok\n"
    assert events == ["refresh"]

    def refuse() -> None:
        message = "could not get a fresh GitHub credential before push"
        raise pr_tools.Invalid(message)

    monkeypatch.setattr(pr_tools.gh_refresh, "refresh_if_served", refuse)
    with pytest.raises(SystemExit, match="could not get a fresh GitHub credential"):
        pr_tools.run([str(gh), "pr", "create"])
