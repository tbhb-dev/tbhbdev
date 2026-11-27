# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for pull request check results."""

import pytest

from tbhbdev.alpha.pull_request import check_results as pr_check_results


@pytest.mark.parametrize(
    "page",
    [{}, {"data": []}, {"data": {"repository": {"object": None}}}],
)
def test_check_nodes_refuses_malformed_graphql(page: dict[str, object]) -> None:
    with pytest.raises(
        SystemExit, match=r"^cannot read checks: malformed GraphQL response$"
    ):
        pr_check_results.check_nodes(page)


def test_check_nodes_accepts_a_null_rollup() -> None:
    page: dict[str, object] = {
        "data": {"repository": {"object": {"statusCheckRollup": None}}}
    }
    assert pr_check_results.check_nodes(page) == []
