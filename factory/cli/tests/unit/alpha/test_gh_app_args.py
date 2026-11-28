# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""GitHub App command argument parsing."""

from dataclasses import replace

import pytest

from tbhbdev.alpha import (
    gh_app_args,
)

REPO = "acme/widget"

BASE = gh_app_args.Args(
    app="bot",
    repo=REPO,
    fresh=False,
    dry_run=False,
    all_repos=False,
    vault="tbhb-dev",
    cooldown_until=None,
    cooldown_status=False,
)


def test_parse_args_fills_every_field() -> None:
    assert gh_app_args.parse_args(["bot", REPO]) == BASE
    full = gh_app_args.parse_args(
        ["bot", REPO, "--fresh", "--dry-run", "--all-repos", "--vault", "v"]
    )
    assert full == replace(BASE, fresh=True, dry_run=True, all_repos=True, vault="v")
    until = gh_app_args.parse_args(["bot", "--cooldown-until", "5"])
    assert until == replace(BASE, repo="", cooldown_until=5)
    status = gh_app_args.parse_args(["bot", "--cooldown-status"])
    assert status == replace(BASE, repo="", cooldown_status=True)


@pytest.mark.parametrize("app", ["a", "ab-", "Ab", "a_b", "a.b", "a b", ""])
def test_a_bad_app_is_a_usage_error(app: str) -> None:
    with pytest.raises(gh_app_args.UsageError, match="must match"):
        gh_app_args.parse_args([app, REPO])


@pytest.mark.parametrize("app", ["ab", "a1", "my-app-2"])
def test_a_good_app_parses(app: str) -> None:
    assert gh_app_args.parse_args([app, REPO]).app == app


@pytest.mark.parametrize(
    "repo",
    ["acme", "a/b/c", "../r", "o/..", "./r", "o/.", "o/r r", "o/r;x", "/r", "o/", ""],
)
def test_a_bad_repo_is_a_usage_error(repo: str) -> None:
    with pytest.raises(gh_app_args.UsageError, match="OWNER/REPO"):
        gh_app_args.parse_args(["bot", repo])


def test_dots_are_fine_inside_a_repo_name() -> None:
    assert gh_app_args.parse_args(["bot", "a.b/c..d"]).repo == "a.b/c..d"
    assert gh_app_args.parse_args(["bot", ".a/b."]).repo == ".a/b."


@pytest.mark.parametrize("vault", ["", ".", "..", "a/b", "../x", "/", "a\0b"])
def test_a_vault_that_cannot_be_one_path_component_is_a_usage_error(
    vault: str,
) -> None:
    assert not gh_app_args.valid_vault(vault)
    with pytest.raises(gh_app_args.UsageError, match="cannot be a directory name"):
        gh_app_args.parse_args(["bot", REPO, "--vault", vault])
    with pytest.raises(gh_app_args.UsageError, match="cannot be a directory name"):
        gh_app_args.parse_args(["bot", "--cooldown-status", "--vault", vault])


@pytest.mark.parametrize("vault", ["tbhb-dev", "My Vault", "a.b", "..a", "a..", "+"])
def test_any_other_vault_is_one_path_component(vault: str) -> None:
    assert gh_app_args.valid_vault(vault)
    assert gh_app_args.parse_args(["bot", REPO, "--vault", vault]).vault == vault


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["-ab", REPO],
        ["bot", REPO, "--nope"],
        ["bot", REPO, "extra"],
        ["bot", "--cooldown-until"],
        ["bot", "--cooldown-until", "abc"],
        ["bot", "--cooldown-until", "-1"],
        ["bot", "--cooldown-until", "١٢٣"],
        ["bot", "--cooldown-until", "100000000000"],
        ["bot", REPO, "--cooldown-until", "5"],
        ["bot", "--cooldown-status", "--fresh"],
        ["bot", "--cooldown-status", "--dry-run"],
        ["bot", "--cooldown-until", "5", "--all-repos"],
    ],
)
def test_other_bad_command_lines_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(gh_app_args.UsageError):
        gh_app_args.parse_args(argv)


@pytest.mark.parametrize(
    ("text", "epoch"),
    [
        (None, None),
        ("", None),
        ("12\n", 12),
        (" 12 ", 12),
        ("0", 0),
        ("99999999999", 99999999999),
        ("100000000000", None),
        ("-1", None),
        ("+1", None),
        ("1_0", None),
        ("1.5", None),
        ("١٢", None),
        ("12x", None),
        ("x12", None),
    ],
)
def test_parse_epoch(text: str | None, epoch: int | None) -> None:
    assert gh_app_args.parse_epoch(text) == epoch


def test_permissions_parse_and_sort() -> None:
    args = gh_app_args.parse_args(
        ["bot", REPO, "--permissions", "pull_requests:write,contents:read"]
    )
    assert args.permissions == (("contents", "read"), ("pull_requests", "write"))
    assert gh_app_args.parse_args(["bot", REPO]).permissions == ()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "contents",
        "contents:admin",
        "Contents:read",
        "contents:read,contents:write",
        "made_up_permission:read",
    ],
)
def test_bad_permissions_are_refused(value: str) -> None:
    with pytest.raises(gh_app_args.UsageError, match="--permissions entry"):
        gh_app_args.parse_args(["bot", REPO, "--permissions", value])


def test_a_repeated_permissions_option_is_refused() -> None:
    with pytest.raises(gh_app_args.UsageError, match="given once"):
        gh_app_args.parse_args(
            ["bot", REPO, "--permissions", "contents:read", "--permissions", "bad"]
        )


def test_a_cooldown_option_refuses_permissions() -> None:
    with pytest.raises(gh_app_args.UsageError, match="takes only the app"):
        gh_app_args.parse_args(["bot", "--cooldown-status", "--permissions", "a:read"])


def test_the_owner_is_what_precedes_the_first_slash() -> None:
    assert gh_app_args.split_repo("o/r") == ("o", "r")
    assert gh_app_args.split_repo("a/b/c") == ("a", "b/c")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["Bad_App", REPO], "app 'Bad_App' must match [a-z0-9][a-z0-9-]*[a-z0-9]"),
        (["bot", "nope"], "repository 'nope' must be OWNER/REPO"),
        (["bot", "o/.."], "repository 'o/..' must be OWNER/REPO"),
        (["bot", "./r"], "repository './r' must be OWNER/REPO"),
        (["bot", "o/.", "--vault", "v"], "repository 'o/.' must be OWNER/REPO"),
        (["bot", REPO, "--vault", ".."], "vault '..' cannot be a directory name"),
        (["bot"], "OWNER/REPO is required"),
        (
            ["bot", "--cooldown-until", "x"],
            "--cooldown-until needs a whole number of seconds since the epoch",
        ),
        (
            ["bot", "--cooldown-until", "5", "--cooldown-status"],
            "--cooldown-until and --cooldown-status are exclusive",
        ),
        (["bot", REPO, "--cooldown-status"], "a cooldown option takes only the app"),
        (
            ["bot", "--cooldown-until", "5", "--fresh"],
            "a cooldown option takes only the app",
        ),
    ],
)
def test_usage_errors_say_what_is_wrong(argv: list[str], message: str) -> None:
    with pytest.raises(gh_app_args.UsageError) as caught:
        gh_app_args.parse_args(argv)
    assert str(caught.value) == message


def test_the_parser_raises_where_argparse_would_print_help() -> None:
    with pytest.raises(gh_app_args.HelpRequestedError):
        gh_app_args.Parser().print_help()
