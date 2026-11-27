# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Exercise script write detection and source reads."""

import os
from pathlib import Path

import pytest
from unit.alpha.guard.scripts.fixtures import (
    SHEBANG,
    bash,
    env_of,
    outside,
    patch,
    places,
    tool,
)

from tbhbdev.alpha.guard.scripts import writes
from tbhbdev.alpha.guard.scripts.filesystem import first_refusal
from tbhbdev.alpha.guard.scripts.paths import TMP_ROOT, Write

type Json = dict[str, object]


PYTHON_FIRST_LINE = "#!/usr/bin/env -S python3 -I"
PYTHON_NEW = "2026-11-11-coord-foo.py"


@pytest.mark.parametrize(
    ("command", "written"),
    [
        ("echo x > ~/.cache/coord/x/run.sh", ".cache/coord/x/run.sh"),
        ("echo x >> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >| ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >|~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &>~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &>> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 2> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 12>~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 2>&1 > ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >&~/.cache/a.sh", ".cache/a.sh"),
        ("> ~/.cache/a.sh", ".cache/a.sh"),
        ("A=1 cat > ~/.cache/a.sh", ".cache/a.sh"),
        ("> ~/.cache/a.sh cat", ".cache/a.sh"),
        ("echo x | tee ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | tee -a ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | tee /dev/null ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | sudo tee ~/.cache/a.sh", ".cache/a.sh"),
        ("cp ./t.sh ~/.cache/", ".cache/t.sh"),
        ("cp t.sh ~/.cache/renamed", ".cache/renamed"),
        ("cp -a t.sh ~/.cache/x", ".cache/x"),
        ("cp -t ~/.cache t.sh", ".cache/t.sh"),
        ("cp -t~/.cache t.sh", ".cache/t.sh"),
        ("cp --target-directory=~/.cache t.sh", ".cache/t.sh"),
        ("cp --target-directory ~/.cache t.sh", ".cache/t.sh"),
        ("cp -t ~/.cache a.sh b.sh", ".cache/a.sh"),
        ("cp a.sh b.sh ~/.cache", ".cache/a.sh"),
        ("cp a.sh b.sh ~/.cache", ".cache/b.sh"),
        ("mv t.sh ~/.cache/", ".cache/t.sh"),
        ("mv t.sh ~/Desktop/x.sh", "Desktop/x.sh"),
        ("install -m755 t.sh ~/bin/x.sh", "bin/x.sh"),
        ("install -m 755 t.sh ~/bin/x", "bin/x"),
        ("install -o me -g us t.sh ~/bin/x", "bin/x"),
        ("install -D -m755 t.sh ~/bin/", "bin/t.sh"),
        ("ln -s ~/x.sh ~/.cache/link", ".cache/link"),
        ("ln t.sh ~/.cache/link", ".cache/link"),
        ("ln -sf t.sh ~/.cache/", ".cache/t.sh"),
        ("ln -t ~/.cache t.sh", ".cache/t.sh"),
        ("bash -c 'echo x > ~/.cache/a.sh'", ".cache/a.sh"),
        ('sh -lc "cp t.sh ~/.cache/"', ".cache/t.sh"),
        ("eval 'echo x > ~/.cache/a.sh'", ".cache/a.sh"),
        ("sudo -E tee ~/.cache/a.sh", ".cache/a.sh"),
        ("cd /x && echo x > ~/.cache/a.sh", ".cache/a.sh"),
        ("echo $(echo x > ~/.cache/a.sh)", ".cache/a.sh"),
        ("echo x > $HOME/.cache/a.sh", ".cache/a.sh"),
        ("echo x > ${HOME}/.cache/a.sh", ".cache/a.sh"),
        ("echo x > ~/.cache/../.cache/a.sh", ".cache/a.sh"),
        ("echo x > ../.cache/a.sh", ".cache/a.sh"),
        ("echo x > ../.cache/a.sh && true", ".cache/a.sh"),
        ("echo x > ~/.local/state/tbhb/a.sh", ".local/state/tbhb/a.sh"),
        ("echo x > ~/.local/libexec/other/a.sh", ".local/libexec/other/a.sh"),
        ("echo x > ~", ""),
        ("cp ~/.cache/a.sh .", "work/a.sh"),
    ],
)
def test_a_script_under_home_is_refused(home: Path, command: str, written: str) -> None:
    (home / "work" / "t.sh").write_text("")
    if written in {"", ".cache/a.sh"} and "~/.cache/a.sh" not in command:
        pass
    # `echo x > ~` writes the home directory itself, which isn't a script.
    expected = None if written == "" else outside(f"{home}/{written}")
    found = first_refusal(bash(home, command), env_of(home))
    if "cp a.sh b.sh" in command:
        assert found in {outside(f"{home}/.cache/a.sh"), outside(f"{home}/.cache/b.sh")}
    else:
        assert found == expected


def test_both_copies_are_judged(home: Path) -> None:
    place = places(home)
    found = list(writes.bash_writes("cp a.sh b.sh ~/.cache", place))
    assert found == [
        Write(f"{home}/.cache/a.sh", script=True),
        Write(f"{home}/.cache/b.sh", script=True),
    ]


def test_a_heredoc_that_starts_with_a_shebang_makes_a_script(home: Path) -> None:
    command = "cat > ~/.cache/x <<'E'\n#!/bin/sh\necho hi\nE\n"
    found = first_refusal(bash(home, command), env_of(home))
    assert found == outside(f"{home}/.cache/x")
    tee = "cat <<E | tee ~/.cache/x\n#!/bin/sh\nE\n"
    assert first_refusal(bash(home, tee), env_of(home)) is None
    fed = "tee ~/.cache/x <<E\n#!/bin/sh\nE\n"
    assert first_refusal(bash(home, fed), env_of(home)) == outside(f"{home}/.cache/x")
    first = "cat > ~/.cache/x <<'E'\necho hi\n#!/bin/sh\nE\n"
    assert first_refusal(bash(home, first), env_of(home)) is None
    dash = "cat > ~/.cache/x <<-E\n#!/bin/sh\nE\n"
    assert first_refusal(bash(home, dash), env_of(home)) == outside(f"{home}/.cache/x")


def test_a_heredoc_fed_to_a_shell_is_scanned(home: Path) -> None:
    command = "bash <<'E'\necho x > ~/.cache/a.sh\nE\n"
    assert first_refusal(bash(home, command), env_of(home)) == outside(
        f"{home}/.cache/a.sh"
    )


def test_ssh_runs_elsewhere_so_it_is_not_judged(home: Path) -> None:
    command = "ssh host 'cat > ~/.cache/a.sh'"
    assert first_refusal(bash(home, command), env_of(home)) is None
    assert (
        first_refusal(bash(home, "ssh host <<E\n> ~/a.sh\nE\n"), env_of(home)) is None
    )


def test_a_shebang_file_without_a_sh_name_is_a_script(home: Path) -> None:
    state = home / ".local" / "state" / "foo"
    state.mkdir(parents=True)
    path = state / "run"
    found = first_refusal(
        tool(home, "Write", file_path=str(path), content=SHEBANG), env_of(home)
    )
    assert found == outside(path)
    plain = tool(home, "Write", file_path=str(path), content="#\n")
    assert first_refusal(plain, env_of(home)) is None
    assert first_refusal(tool(home, "Write", file_path=str(path)), env_of(home)) is None


def test_a_copied_source_is_read_for_its_shebang(home: Path) -> None:
    work = home / "work"
    (work / "t").write_text(SHEBANG + "echo\n")
    (work / "plain").write_text("#\n")
    (work / "one").write_text("#")
    (work / "empty").write_text("")
    (work / "dir").mkdir()
    env = env_of(home)
    found = first_refusal(bash(home, "cp t ~/.cache/x"), env)
    assert found == outside(f"{home}/.cache/x")
    for source in ["plain", "one", "empty", "dir", "missing"]:
        assert first_refusal(bash(home, f"cp {source} ~/.cache/x"), env) is None
    found = first_refusal(bash(home, "mv ~/work/t ~/.cache/x"), env)
    assert found == outside(f"{home}/.cache/x")


def test_a_fifo_source_is_not_opened(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = home / "work" / "pipe"
    os.mkfifo(fifo)

    def unexpected_open(_path: Path, *_args: object, **_kwargs: object) -> None:
        pytest.fail("a FIFO source must never be opened")

    monkeypatch.setattr(Path, "open", unexpected_open)
    command = "cp pipe ~/.cache/x"
    assert first_refusal(bash(home, command), env_of(home)) is None
    assert writes.read_first_line(str(fifo), python=False) is None


def test_an_unreadable_source_is_not_a_script(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "work" / "dir").mkdir()

    def is_file(_self: Path) -> bool:
        return True

    monkeypatch.setattr(Path, "is_file", is_file)
    assert writes.read_first_line(str(home / "work" / "dir"), python=False) is None


def test_non_python_source_reads_only_two_bytes(home: Path) -> None:
    path = home / "f"
    path.write_text("#!/bin/sh\n")
    assert writes.read_first_line(str(path), python=False) == "#!"
    path.write_text("#/")
    assert writes.read_first_line(str(path), python=False) != "#!"
    path.write_text("!#")
    assert writes.read_first_line(str(path), python=False) != "#!"


@pytest.mark.parametrize(
    "command",
    [
        "echo x > ~/.cache/x.json",
        "echo x > ~/.cache/x",
        "cp t.json ~/.cache/",
        "cp t ~/.cache/",
        "cp",
        "cp a",
        "cp -r",
        "ln -s",
        "install -d ~/.cache/x.sh",
        "install -d ~/.cache/x.sh ~/.cache/y.sh",
        "install --directory ~/.cache/x.sh",
        "echo x 2>&1",
        "echo x >&2",
        "echo x 2>&-",
        "echo x >&-",
        "echo x > /dev/null",
        "echo x 2> /dev/null",
        "echo x >",
        "echo x > ",
        "cat ~/.cache/a.sh",
        "ls ~/.cache/a.sh",
        "chmod +x ~/.cache/a.sh",
        "bash ~/.cache/a.sh",
        "echo x | tee",
        "echo x | tee -a",
        "echo '> ~/.cache/a.sh'",
        "echo '#!/bin/sh' ~/.cache/x",
        "echo x > /usr/local/bin/x.sh",
        "echo x > /var/folders/x/T/a.sh",
        "echo x > /etc/a.sh",
        "echo x > /tmp2/a.sh",
        "echo x > /private/tmp2/a.sh",
        "echo x > /private/a.sh",
        "echo x # > ~/.cache/a.sh",
        "bash -c",
        "bash -c ''",
        "bash script.sh",
        "eval",
        "",
        "   ",
        "true",
        "A=1",
        "sudo",
    ],
)
def test_these_commands_are_allowed(home: Path, command: str) -> None:
    assert first_refusal(bash(home, command), env_of(home)) is None


def test_redirect_scanner() -> None:
    cases = {
        "cat >f": [["cat", ">", "f"]],
        "cat > f": [["cat", ">", "f"]],
        "cat >> f": [["cat", ">>", "f"]],
        "cat >| f": [["cat", ">|", "f"]],
        "cat &> f": [["cat", "&>", "f"]],
        "cat &>> f": [["cat", "&>>", "f"]],
        "cat 2>&1": [["cat", ">&", "1"]],
        "cat 2> f": [["cat", ">", "f"]],
        "cat 12>f": [["cat", ">", "f"]],
        "a && b": [["a"], ["b"]],
        "a & b": [["a"], ["b"]],
        "a | b": [["a"], ["b"]],
        "a ; b > f": [["a"], ["b", ">", "f"]],
        "cat 'a>b'": [["cat", "a>b"]],
        "cat <f": [["cat", "<f"]],
    }
    for text, expected in cases.items():
        assert writes.RedirectScanner(text).run() == expected, text
    assert all(
        isinstance(word, writes.Redirect)
        for word in writes.RedirectScanner("cat >f").run()[0][1:2]
    )
    assert not isinstance(writes.RedirectScanner("cat >f").run()[0][2], writes.Redirect)
    assert not isinstance(writes.RedirectScanner("cat >f").run()[0][0], writes.Redirect)


def test_redirects_split_targets_from_words() -> None:
    redirect = writes.Redirect
    assert writes.redirects(["a", redirect(">"), "f", "b"]) == (["f"], ["a", "b"])
    assert writes.redirects([redirect(">&"), "2", "a"]) == ([], ["a"])
    assert writes.redirects([redirect(">&"), "-", "a"]) == ([], ["a"])
    assert writes.redirects([redirect(">&"), "2-"]) == ([], [])
    assert writes.redirects([redirect(">&"), "f"]) == (["f"], [])
    assert writes.redirects([redirect(">"), "2"]) == (["2"], [])
    assert writes.redirects([redirect(">")]) == ([], [])
    assert writes.redirects([redirect(">&"), "2x"]) == (["2x"], [])


def test_apply_patch_paths(home: Path) -> None:
    place = places(home)
    text = patch(
        "*** Add File: a.sh",
        "+echo",
        "*** Add File: shebang",
        "+#!/bin/sh",
        "*** Add File: second",
        "+echo",
        "+#!/bin/sh",
        "*** Add File: empty",
        "*** Add File: last",
        "*** Update File: u.sh",
        "@@",
        "-a",
        "+b",
        "*** Update File: v",
        "*** Move to: moved",
        "*** Update File: w.sh",
        "*** Move to: moved.sh",
        "*** Update File: x",
        "*** Move to: y.sh",
        "*** Update File: m.sh",
        "*** Move to: m2",
        "*** Add File: odd: name.sh",
        "*** Delete File: gone.sh",
    )
    w = str(home / "work")
    assert writes.patch_writes(text, place) == [
        Write(f"{w}/a.sh", script=True),
        Write(f"{w}/shebang", script=True),
        Write(f"{w}/second", script=False),
        Write(f"{w}/empty", script=False),
        Write(f"{w}/last", script=False),
        Write(f"{w}/u.sh", script=True),
        Write(f"{w}/v", script=False),
        Write(f"{w}/moved", script=False),
        Write(f"{w}/w.sh", script=True),
        Write(f"{w}/moved.sh", script=True),
        Write(f"{w}/x", script=False),
        Write(f"{w}/y.sh", script=True),
        Write(f"{w}/m.sh", script=True),
        Write(f"{w}/m2", script=True),
        Write(f"{w}/odd: name.sh", script=True),
    ]
    assert writes.patch_writes("", place) == []
    assert writes.patch_writes("*** Move to: z", place) == []
    assert writes.patch_writes("+*** Add File: a.sh", place) == []


def test_tool_call_shapes() -> None:
    assert writes.tool_call({"tool_name": "Write", "tool_input": {"a": 1}}) == (
        "Write",
        {"a": 1},
    )
    assert writes.tool_call({"tool_name": 5, "tool_input": []}) == (5, {})
    assert writes.tool_call({"toolCall": {"name": "n", "args": '{"a": 1}'}}) == (
        "n",
        {"a": 1},
    )
    assert writes.tool_call({"toolCall": {"name": "n", "args": {"a": 1}}}) == (
        "n",
        {"a": 1},
    )
    assert writes.tool_call({"toolCall": {"name": "n", "args": "{"}}) == ("n", {})
    assert writes.tool_call(
        {"toolCall": 3, "tool_name": "x", "tool_input": {"b": 2}}
    ) == ("x", {"b": 2})


def test_first_str() -> None:
    assert writes.first_str({"a": 1, "b": "x", "c": "y"}, ("a", "b", "c")) == "x"
    assert writes.first_str({"a": ""}, ("a", "b")) == ""
    assert writes.first_str({}, ("a",)) == ""
    assert writes.first_str({"b": "z"}, ("a", "b")) == "z"


def test_copy_writes(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    cases: list[tuple[str, list[str], list[Write]]] = [
        ("cp", ["a", "b"], [Write(f"{w}/b", script=False)]),
        ("cp", ["a.sh", "b"], [Write(f"{w}/b", script=True)]),
        ("cp", ["a", "b.sh"], [Write(f"{w}/b.sh", script=True)]),
        ("cp", ["a"], []),
        ("cp", [], []),
        ("cp", ["-r", "a"], []),
        (
            "cp",
            ["a", "b", "d/"],
            [
                Write(f"{w}/d/a", script=False),
                Write(f"{w}/d/b", script=False),
            ],
        ),
        ("cp", ["x/a.sh", "d/"], [Write(f"{w}/d/a.sh", script=True)]),
        ("cp", ["-t", "d", "a"], [Write(f"{w}/d/a", script=False)]),
        ("cp", ["-t", "d"], []),
        (
            "cp",
            ["--target-directory=d", "a", "b"],
            [
                Write(f"{w}/d/a", script=False),
                Write(f"{w}/d/b", script=False),
            ],
        ),
        ("ln", ["-s", "x/a.sh"], [Write(f"{w}/a.sh", script=True)]),
        ("ln", ["-s", "a", "b"], [Write(f"{w}/b", script=False)]),
        (
            "ln",
            ["-s", "a", "b", "c"],
            [
                Write(f"{w}/c/a", script=False),
                Write(f"{w}/c/b", script=False),
            ],
        ),
        ("mv", ["a", "b"], [Write(f"{w}/b", script=False)]),
        ("install", ["-m", "755", "a", "b"], [Write(f"{w}/b", script=False)]),
        ("install", ["-m755", "a", "b"], [Write(f"{w}/b", script=False)]),
        ("install", ["-d", "a", "b"], []),
        ("install", ["--directory", "a"], []),
        ("install", ["--directory", "a", "b"], []),
        ("install", ["-D", "a", "b"], [Write(f"{w}/b", script=False)]),
        ("cp", ["-S", ".bak", "a", "b"], [Write(f"{w}/b", script=False)]),
        ("cp", ["--suffix=.bak", "a", "b"], [Write(f"{w}/b", script=False)]),
    ]
    for program, args, expected in cases:
        assert list(writes.copy_writes(program, args, place)) == expected, (
            program,
            args,
        )


def test_an_existing_directory_destination_takes_the_source_name(home: Path) -> None:
    (home / "work" / "d").mkdir()
    place = places(home)
    found = list(writes.copy_writes("cp", ["x.sh", "d"], place))
    assert found == [Write(f"{home}/work/d/x.sh", script=True)]
    found = list(writes.copy_writes("cp", ["x.sh", "e"], place))
    assert found == [Write(f"{home}/work/e", script=True)]
    link = home / "work" / "l"
    link.symlink_to(home / "work" / "d")
    found = list(writes.copy_writes("cp", ["x.sh", "l"], place))
    assert found == [Write(f"{home}/work/l/x.sh", script=True)]


def test_command_writes(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    cases: list[tuple[list[str], str | None, list[Write]]] = [
        (
            ["echo", writes.Redirect(">"), "f.sh"],
            None,
            [Write(f"{w}/f.sh", script=True)],
        ),
        (
            ["echo", writes.Redirect(">"), "f"],
            "#!/bin/sh\n",
            [Write(f"{w}/f", script=True)],
        ),
        (
            ["echo", writes.Redirect(">"), "f"],
            "echo\n",
            [Write(f"{w}/f", script=False)],
        ),
        (
            ["tee", "-a", "f", "g.sh"],
            "#!x\n",
            [Write(f"{w}/f", script=True), Write(f"{w}/g.sh", script=True)],
        ),
        (["tee", "f"], "#!x\n", [Write(f"{w}/f", script=True)]),
        (["tee", "f"], "x\n", [Write(f"{w}/f", script=False)]),
        (["sudo", "-n", "tee", "f.sh"], None, [Write(f"{w}/f.sh", script=True)]),
        (["A=1", "tee", "f.sh"], None, [Write(f"{w}/f.sh", script=True)]),
        (["/usr/bin/tee", "f.sh"], None, [Write(f"{w}/f.sh", script=True)]),
        (["bash", "-c", "echo > f.sh"], None, [Write(f"{w}/f.sh", script=True)]),
        (["eval", "echo", ">", "f.sh"], None, [Write(f"{w}/f.sh", script=True)]),
        (["ssh", "h", "echo > f.sh"], None, []),
        (["/usr/bin/ssh", "h", "echo > f.sh"], None, []),
        (["cat", "f.sh"], None, []),
        ([], None, []),
        (["A=1"], None, []),
    ]
    for command, body, expected in cases:
        assert list(writes.command_writes(command, body, place)) == expected, command


def test_bash_writes_pairs_each_heredoc_with_its_command(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    text = "cat > a <<E\n#!/bin/sh\nE\ncat > b <<E\nx\nE\ncat > c\n"
    assert list(writes.bash_writes(text, place)) == [
        Write(f"{w}/a", script=True),
        Write(f"{w}/b", script=False),
        Write(f"{w}/c", script=False),
    ]


def test_targets(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    assert writes.targets(bash(home, "echo > a.sh"), place) == [
        Write(f"{w}/a.sh", script=True)
    ]
    assert writes.targets(tool(home, "Write", file_path="a"), place) == [
        Write(f"{w}/a", script=False)
    ]
    assert writes.targets(tool(home, "Write", file_path="a", content="#!"), place) == [
        Write(f"{w}/a", script=True)
    ]
    assert writes.targets({}, place) == []
    # A patch's text is not a command.
    data = tool(home, "apply_patch", command="*** Add File: a.sh\n+x")
    assert writes.targets(data, place) == [Write(f"{w}/a.sh", script=True)]


WRITE = "echo x > a.sh"


@pytest.fixture
def repo(home: Path) -> Path:
    path = home / "Code" / "repo"
    (path / ".git").mkdir(parents=True)
    (path / "sub").mkdir()
    return path


@pytest.mark.parametrize(
    ("command", "written"),
    [
        # refused where the cd leaves it, relative to the home directory
        ("cd ~/.cache && {w}", ".cache/a.sh"),
        ("cd $HOME/.cache; {w}", ".cache/a.sh"),
        ("cd ${HOME}/.cache || {w}", ".cache/a.sh"),
        ("cd ~/.cache\n{w}", ".cache/a.sh"),
        ("cd ~/.cache > /dev/null && {w}", ".cache/a.sh"),
        ("cd && {w}", "a.sh"),
        ("cd .. && {w}", "a.sh"),
        ("cd .cache && {w}", "work/.cache/a.sh"),
        ("cd ~/Code/repo && cd ~/.cache && {w}", ".cache/a.sh"),
        ("cd ~/.cache && cd .. && {w}", "a.sh"),
        ("echo $(cd ~/.cache; {w})", ".cache/a.sh"),
        ("bash -c 'cd ~/.cache; {w}'", ".cache/a.sh"),
        ("cd ~/.cache && bash -c '{w}'", ".cache/a.sh"),
        # a cd in a ( ) group or a substitution is over when it closes
        ("(cd ~/.cache) && {w}", "work/a.sh"),
        ("(cd ~/Code/repo) && {w}", "work/a.sh"),
        ("echo $(cd ~/Code/repo) && {w}", "work/a.sh"),
        ("echo `cd ~/Code/repo` && {w}", "work/a.sh"),
        # a cd that can't be followed leaves the directory the text began in
        ("cd - && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd - && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd -P && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd $ELSEWHERE && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd ${ELSEWHERE} && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd $(mktemp -d) && {w}", "work/a.sh"),
        ('cd ~/Code/repo && cd "$(mktemp -d)" && {w}', "work/a.sh"),
        ("cd ~/Code/repo && cd `mktemp -d` && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd ~other && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd -P ~/Code/repo && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd a b && {w}", "work/a.sh"),
        # allowed: a work tree, as tracked, whatever the cwd says
        ("cd ~/Code/repo && {w}", None),
        ("cd ~/Code/repo; {w}", None),
        ("cd ~/Code/repo > /dev/null && {w}", None),
        ("builtin cd ~/Code/repo && {w}", None),
        ("cd ~/Code/repo && cd sub && {w}", None),
        ("cd ~/Code/repo && (cd ~/.cache; true) && {w}", None),
        ("cd ~/Code/repo && ({w})", None),
        ("cd ~/Code/repo && echo $(true) && {w}", None),
        ("echo $(true) && cd ~/Code/repo && {w}", None),
        ("bash -c 'cd ~/Code/repo && {w}'", None),
        ("cd ~/Code/repo && bash -c '{w}'", None),
        ("cd ~/Code/repo && echo x > a.txt", None),
    ],
)
def test_a_cd_moves_later_writes(
    home: Path, repo: Path, command: str, written: str | None
) -> None:
    assert repo.is_dir()
    found = first_refusal(
        bash(home, command.replace("{w}", WRITE)), env_of(home, TMP_ROOT)
    )
    assert found == (None if written is None else outside(f"{home}/{written}"))


def test_a_cd_to_an_unset_tmpdir_leaves_the_start(home: Path, repo: Path) -> None:
    command = "cd ~/Code/repo && cd $TMPDIR && echo x > a.sh"
    assert repo.is_dir()
    found = first_refusal(bash(home, command), env_of(home))
    assert found == outside(f"{home}/work/a.sh")


def test_a_cd_tracked_to_a_run_scoped_tmpdir(home: Path) -> None:
    command = "cd $TMPDIR && echo x > a.sh"
    assert first_refusal(bash(home, command), env_of(home, f"{TMP_ROOT}/run-1")) is None
    assert first_refusal(bash(home, command), env_of(home, TMP_ROOT)) == outside(
        f"{TMP_ROOT}/a.sh"
    )


def test_a_tmpdir_in_a_path_expands(home: Path) -> None:
    for command in (
        'cat > "$TMPDIR/run.sh"',
        "cat > ${TMPDIR}/run.sh",
        "cp t.sh $TMPDIR/run.sh",
        "echo x | tee $TMPDIR/run.sh",
    ):
        (home / "work" / "t.sh").write_text("")
        found = first_refusal(bash(home, command), env_of(home, f"{TMP_ROOT}/run-1"))
        assert found is None, command
        found = first_refusal(bash(home, command), env_of(home, TMP_ROOT))
        assert found == outside(f"{TMP_ROOT}/run.sh"), command
    data = tool(home, "Write", file_path="$TMPDIR/run.sh")
    assert first_refusal(data, env_of(home, f"{TMP_ROOT}/run-1")) is None
    assert first_refusal(data, env_of(home, TMP_ROOT)) == outside(f"{TMP_ROOT}/run.sh")


@pytest.mark.parametrize("tmpdir", [None, ""])
def test_an_unset_tmpdir_leaves_the_path_literal(
    home: Path, tmpdir: str | None
) -> None:
    work = home / "work"
    for raw in ("$TMPDIR", "${TMPDIR}"):
        command = f'cat > "{raw}/run.sh"'
        found = first_refusal(bash(home, command), env_of(home, tmpdir))
        assert found == outside(f"{work}/{raw}/run.sh")


def test_python_empty_patch_is_known(home: Path, libexec: Path) -> None:
    path = libexec / PYTHON_NEW
    for text in (f"*** Add File: {path}", patch(f"*** Add File: {path}")):
        data = tool(home, "apply_patch", patch=text)
        assert writes.patch_writes(text, places(home))[0].first_line == (
            "" if text.endswith(PYTHON_NEW) else "*** End Patch"
        )
        assert (
            first_refusal(data, env_of(home))
            == f"{path} must start with {PYTHON_FIRST_LINE}."
        )
