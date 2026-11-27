# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request metadata."""

import pytest

from tbhbdev.alpha.pull_request import metadata as pr_metadata


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/tbhb-dev/tbhbdev/pull/12",
        "https://github.com/o/n/pull/0",
        "https://github.com/A1-b/.github/pull/123456",
    ],
)
def test_a_pr_url_names_its_repository(url: str) -> None:
    assert (
        pr_metadata.target_of_pr(url)
        == url.removeprefix("https://github.com/").split("/pull/")[0]
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        7,
        ["https://github.com/o/n/pull/1"],
        "",
        "not a url",
        "http://github.com/o/n/pull/1",
        "https://gitlab.com/o/n/pull/1",
        "https://githubXcom/o/n/pull/1",
        "https://github.com.evil.example/o/n/pull/1",
        "https://www.github.com/o/n/pull/1",
        "https://user@github.com/o/n/pull/1",
        "https://github.com:443/o/n/pull/1",
        "xhttps://github.com/o/n/pull/1",
        "https://github.com/o/n/pull/",
        "https://github.com/o/n/pull/x",
        "https://github.com/o/n/pull/1x",
        "https://github.com/o/n/pull/\u0661",
        "https://github.com/o/n/issues/1",
        "https://github.com/o/n/pulls/1",
        "https://github.com/o/n/pull/1/files",
        "https://github.com/o/n/pull/1?x=1",
        "https://github.com/o/n/pull/1#c",
        "https://github.com/o/n/pull/1\n",
        " https://github.com/o/n/pull/1",
        "https://github.com/o/../pull/1",
        "https://github.com/o/./pull/1",
        "https://github.com/o//pull/1",
        "https://github.com//n/pull/1",
        "https://github.com/pull/1",
        "https://github.com/-o/n/pull/1",
        "https://github.com/o/n/x/pull/1",
        "https://github.com/https://github.com/o/n/pull/1",
    ],
)
def test_a_url_that_is_not_a_github_pr_page_names_nothing(url: object) -> None:
    assert pr_metadata.target_of_pr(url) == ""


def test_gh_args_without_metadata() -> None:
    assert pr_metadata.gh_args({}, "main") == ["--base", "main"]
    assert pr_metadata.gh_args({"draft": False}, "main") == ["--base", "main"]
