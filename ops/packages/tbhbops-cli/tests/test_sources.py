"""Tests for the sources: GitHub, tailscale and the word list."""

import json
import sys
lazy from collections.abc import Mapping
lazy from pathlib import Path

import pytest
from tbhbops_cli import sources
from tbhbops_cli.common import CliError
from tbhbops_cli.gitleaks import TECH_WORDS, Tailnet

# A listing that asks this many times has looped, which a mutant can cause.
RUNAWAY = 50


class GitHub:
    """A fake GraphQL endpoint: each owner has pages of repository names."""

    def __init__(self, pages: dict[str, list[list[str]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, Mapping[str, str], dict[str, object]]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert body is not None
        request = json.loads(body)
        self.calls.append((method, url, headers, request))
        assert len(self.calls) < RUNAWAY, "the listing keeps asking for pages"
        variables = request["variables"]
        pages = self.pages[variables["owner"]]
        index = 0 if variables["after"] is None else int(variables["after"])
        nodes = [{"name": n} for n in pages[index]]
        last = index == len(pages) - 1
        info = {"hasNextPage": not last, "endCursor": None if last else str(index + 1)}
        repositories = {"nodes": nodes, "pageInfo": info}
        answer = {"data": {"repositoryOwner": {"repositories": repositories}}}
        return 200, json.dumps(answer).encode()


def canned(status: int, answer: object) -> sources.Http:
    calls: list[None] = []

    def http(*_args: object) -> tuple[int, bytes]:
        calls.append(None)
        assert len(calls) < RUNAWAY, "the listing keeps asking for pages"
        raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return status, raw

    return http


def owned(**repositories: object) -> dict[str, object]:
    return {"data": {"repositoryOwner": {"repositories": repositories}}}


def test_private_repositories_are_listed_page_by_page_for_each_owner() -> None:
    github = GitHub({"o1": [["a", "b"], ["c"]], "o2": [["d"]], "o3": [[]]})
    found = sources.private_repos(github, "the-token", ("o1", "o2", "o3"))
    assert found == [("o1", "a"), ("o1", "b"), ("o1", "c"), ("o2", "d")]
    assert [c[3]["variables"] for c in github.calls] == [
        {"owner": "o1", "after": None, "first": 100},
        {"owner": "o1", "after": "1", "first": 100},
        {"owner": "o2", "after": None, "first": 100},
        {"owner": "o3", "after": None, "first": 100},
    ]


def test_the_request_is_a_graphql_post_with_the_token_as_a_bearer() -> None:
    github = GitHub({"o1": [["a"]]})
    sources.private_repos(github, "the-token", ("o1",))
    ((method, url, headers, body),) = github.calls
    assert (method, url) == ("POST", "https://api.github.com/graphql")
    assert headers == {
        "Authorization": "Bearer the-token",
        "Content-Type": "application/json",
        "User-Agent": "tbhbops",
    }
    assert body["query"] == sources.QUERY
    assert "privacy: PRIVATE" in sources.QUERY
    assert "ownerAffiliations: OWNER" in sources.QUERY


def test_the_first_owner_must_have_private_repositories() -> None:
    with pytest.raises(CliError) as raised:
        sources.private_repos(GitHub({"o1": [[]], "o2": [["a"]]}), "t", ("o1", "o2"))
    assert str(raised.value) == (
        "the GitHub repository list for o1 has no private repositories"
    )


def pages_of(count: int) -> list[list[str]]:
    names = [f"r{n}" for n in range(count)]
    return [names[at : at + 100] for at in range(0, count, 100)]


def test_cursor_paging_continues_past_a_thousand_repositories() -> None:
    github = GitHub({"o1": pages_of(1001), "o2": pages_of(600)})
    found = sources.private_repos(github, "t", ("o1", "o2"))
    assert len(found) == 1601
    assert found[1000] == ("o1", "r1000")


def test_a_failing_request_is_reported_by_status_alone() -> None:
    with pytest.raises(CliError) as raised:
        sources.private_repos(canned(401, b"bad credentials"), "t", ("o1",))
    assert str(raised.value) == "the GitHub repository list for o1 failed: HTTP 401"


def test_an_answer_that_is_not_json_is_refused_without_quoting_it() -> None:
    with pytest.raises(CliError) as raised:
        sources.private_repos(canned(200, b"<html>secret"), "t", ("o1",))
    assert str(raised.value) == (
        "the GitHub repository list for o1 printed something other than JSON"
    )
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    "answer",
    [
        [],
        {},
        {"data": None},
        {"data": {"repositoryOwner": None}},
        owned(),
        owned(nodes="x"),
        owned(nodes=[{"name": 3}]),
        owned(nodes=[{"name": ""}]),
        owned(nodes=[{"other": "a"}]),
        owned(nodes=["a"]),
        owned(nodes=[{"name": "a"}, {"name": None}]),
    ],
)
def test_an_answer_without_named_repositories_is_refused(answer: object) -> None:
    with pytest.raises(CliError, match="something other than named repositories"):
        sources.private_repos(canned(200, answer), "t", ("o1",))


@pytest.mark.parametrize("cursor", [None, "", 3])
def test_a_next_page_without_a_cursor_is_refused(cursor: object) -> None:
    info = {"hasNextPage": True, "endCursor": cursor}
    answer = owned(nodes=[{"name": "a"}], pageInfo=info)
    with pytest.raises(CliError, match="something other than named repositories"):
        sources.private_repos(canned(200, answer), "t", ("o1",))


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_only_a_true_has_next_page_asks_for_another(flag: object) -> None:
    info = {"hasNextPage": flag, "endCursor": "c"}
    answer = owned(nodes=[{"name": "a"}], pageInfo=info)
    assert sources.private_repos(canned(200, answer), "t", ("o1",)) == [("o1", "a")]


def test_a_command_runs_from_an_argv_list_and_returns_its_output() -> None:
    argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", "a b; $(c)"]
    assert sources.run_command(argv) == "a b; $(c)\n"


def test_a_failing_command_reports_its_status_and_none_of_its_output() -> None:
    code = (
        "import sys; print('out-secret'); sys.stderr.write('err-secret'); sys.exit(3)"
    )
    with pytest.raises(CliError) as raised:
        sources.run_command([sys.executable, "-c", code])
    assert str(raised.value) == f"{sys.executable} failed: exit 3"


def test_a_command_that_cannot_start_is_reported() -> None:
    with pytest.raises(CliError) as raised:
        sources.run_command(["/no/such/command-here", "secret"])
    assert str(raised.value) == "cannot run /no/such/command-here"
    assert raised.value.__suppress_context__


STATUS = {"MagicDNSSuffix": "tail-ex.net", "CurrentTailnet": {"Name": "ex.example"}}


def with_tailscale(monkeypatch: pytest.MonkeyPatch, found: str | None) -> None:

    def which(_name: str) -> str | None:
        return found

    monkeypatch.setattr(sources.shutil, "which", which)


def test_the_tailnet_comes_from_tailscale_status_and_the_hosts_from_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_tailscale(monkeypatch, "/fake/tailscale")
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return json.dumps(STATUS)

    got = sources.tailnet(run, ("h1", "h2"))
    assert got == Tailnet("tail-ex.net", "ex.example", ("h1", "h2"))
    assert calls == [["/fake/tailscale", "status", "--json"]]


def test_tailscale_is_looked_up_by_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    names: list[str] = []

    def which(name: str) -> str:
        names.append(name)
        return "/t"

    monkeypatch.setattr(sources.shutil, "which", which)
    sources.tailnet(lambda _argv: json.dumps(STATUS), ("h",))
    assert names == ["tailscale"]


def test_the_macos_app_stands_in_when_tailscale_is_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = tmp_path / "Tailscale"
    app.write_text("", encoding="utf-8")
    with_tailscale(monkeypatch, None)
    monkeypatch.setattr(sources, "TAILSCALE_APP", str(app))
    calls: list[list[str]] = []
    sources.tailnet(lambda argv: calls.append(argv) or json.dumps(STATUS), ("h",))
    assert calls == [[str(app), "status", "--json"]]


def test_no_tailscale_at_all_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_tailscale(monkeypatch, None)
    monkeypatch.setattr(sources, "TAILSCALE_APP", str(tmp_path / "missing"))
    with pytest.raises(CliError, match=r"tailscale isn't on PATH and .* doesn't exist"):
        sources.tailnet(lambda _argv: "", ("h",))


@pytest.mark.parametrize(
    ("status", "missing"),
    [
        ({"CurrentTailnet": {"Name": "n"}}, "MagicDNSSuffix"),
        ({"MagicDNSSuffix": "", "CurrentTailnet": {"Name": "n"}}, "MagicDNSSuffix"),
        ({"MagicDNSSuffix": 3, "CurrentTailnet": {"Name": "n"}}, "MagicDNSSuffix"),
        ({"MagicDNSSuffix": "s"}, "CurrentTailnet.Name"),
        (
            {"MagicDNSSuffix": "s", "CurrentTailnet": {"Name": ""}},
            "CurrentTailnet.Name",
        ),
        ({"MagicDNSSuffix": "s", "CurrentTailnet": {"Name": 3}}, "CurrentTailnet.Name"),
        ({"MagicDNSSuffix": "s", "CurrentTailnet": "n"}, "CurrentTailnet.Name"),
        ([], "MagicDNSSuffix"),
    ],
)
def test_a_status_without_the_suffix_or_the_name_is_an_error(
    monkeypatch: pytest.MonkeyPatch, status: object, missing: str
) -> None:
    with_tailscale(monkeypatch, "/t")
    with pytest.raises(CliError) as raised:
        sources.tailnet(lambda _argv: json.dumps(status), ("h",))
    assert str(raised.value) == f"tailscale status has no {missing}"


def test_a_status_that_is_not_json_is_refused_without_quoting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_tailscale(monkeypatch, "/t")
    with pytest.raises(CliError) as raised:
        sources.tailnet(lambda _argv: "secret", ("h",))
    assert str(raised.value) == "tailscale status printed something other than JSON"


def test_common_words_are_lowercased_and_joined_with_the_tech_words(
    tmp_path: Path,
) -> None:
    path = tmp_path / "words"
    path.write_text("Alpha\nBETA\n gamma \n", encoding="utf-8")
    assert (
        sources.common_words(path) == frozenset({"alpha", "beta", "gamma"}) | TECH_WORDS
    )


def test_a_missing_word_list_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CliError) as raised:
        sources.common_words(tmp_path / "nope")
    assert str(raised.value) == f"cannot read {tmp_path / 'nope'}"


def test_a_word_list_that_is_not_text_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "words"
    path.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(CliError, match="cannot read"):
        sources.common_words(path)


def test_an_empty_word_list_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "words"
    path.write_text(" \n", encoding="utf-8")
    with pytest.raises(CliError) as raised:
        sources.common_words(path)
    assert str(raised.value) == f"{path} lists no words"
