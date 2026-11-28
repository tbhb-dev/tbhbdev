# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

import importlib
import json
import string
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture
def decide(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    action = Path.cwd() / ".github/actions/affected-targets"
    monkeypatch.setattr(sys, "path", [str(action), *sys.path])
    return importlib.import_module("affected_targets.decide")


@pytest.mark.parametrize(
    ("event", "want", "query", "reason"),
    [
        ("push", "", "one\ntwo", "no event head to check the checkout head against"),
        ("schedule", "other", "", "a 'schedule' event tests everything"),
        ("", "other", "", "a '' event tests everything"),
        ("merge_group", "other", "", "the checkout head is not the event's head other"),
        ("pull_request", "head", "one\ntwo", "the query is not one line"),
        ("workflow_dispatch", "head", "", ""),
    ],
)
def test_initial_fallback(
    decide: ModuleType, event: str, want: str, query: str, reason: str
) -> None:
    assert decide.initial_reason(event, "head", want, query) == reason


@pytest.mark.parametrize(
    "path",
    [
        "MODULE.bazel",
        "MODULE.bazel.lock",
        ".bazelrc",
        ".bazelrc.user",
        "preset.bazelrc",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "WORKSPACE.extra",
        ".bazelversion",
        "go/MODULE.bazel",
        "go/deps.MODULE.bazel",
        "café/MODULE.bazel",
        ".bazelignore",
        ".bazeliskrc",
        "REPO.bazel",
    ],
)
def test_bazel_configuration_falls_back(decide: ModuleType, path: str) -> None:
    raw = f":100644 100644 0 0 M\0{path}\0"
    assert decide.classify(raw)[0] == f"{path} changed"


@pytest.mark.parametrize(
    ("mode", "kind"), [("160000", "submodule"), ("120000", "symlink")]
)
@pytest.mark.parametrize("before", [False, True])
def test_git_modes_fall_back(
    decide: ModuleType, mode: str, kind: str, before: bool
) -> None:
    modes = f"{mode} 100644" if before else f"100644 {mode}"
    assert decide.classify(f":{modes} 0 0 T\0lib\0")[0] == f"the {kind} lib changed"


@pytest.mark.parametrize(
    ("status", "path", "checks"),
    [
        ("M", "src/main.go", set()),
        ("M", "go/BUILD.bazel", {"//:buildifier_test"}),
        ("M", "lib/rules/go/BUILD.extra", {"@tbhb_rules//:buildifier_test"}),
        ("M", "go/private.BUILD", {"//:buildifier_test"}),
        ("M", "go/defs.bzl", {"//:buildifier_test"}),
        ("M", "go/defs.sky", {"//:buildifier_test"}),
        ("M", "go/defs.star", {"//:buildifier_test"}),
        (
            "M",
            "lib/rules/go/tools/install.sh.tpl",
            {"@tbhb_rules//repo/tests:mktemp_test"},
        ),
        ("M", "lib/rules/go/install.sh", {"@tbhb_rules//repo/tests:mktemp_test"}),
        ("M", "lib/rules/go/config.yml", {"@tbhb_rules//repo/tests:mktemp_test"}),
        (
            "M",
            "lib/rules/go/README.md",
            {"@tbhb_rules//repo/tests:markdown_links_test"},
        ),
        (
            "M",
            "lib/rules/.markdown-links.toml",
            {"@tbhb_rules//repo/tests:markdown_links_test"},
        ),
        (
            "D",
            "lib/rules/go/target.txt",
            {"@tbhb_rules//repo/tests:markdown_links_test"},
        ),
        (
            "A",
            "lib/rules/go/target.txt",
            {"@tbhb_rules//repo/tests:markdown_links_test"},
        ),
        (
            "M",
            "lib/rules/.gitleaks.toml",
            {"@tbhb_rules//lint/tests:gitleaks_configs_test"},
        ),
        (
            "M",
            "lib/rules/go/.gitleaks.toml",
            {"@tbhb_rules//lint/tests:gitleaks_configs_test"},
        ),
        (
            "M",
            "lib/rules/lint/gitleaks/config",
            {"@tbhb_rules//lint/tests:gitleaks_configs_test"},
        ),
        ("M", "LICENSES/MIT.txt", {"@tbhb_rules//lint/tests:gitleaks_configs_test"}),
        (
            "M",
            "lib/rules/pins/extensions.bzl",
            {
                "@tbhb_rules//lint/tests:gitleaks_configs_test",
                "@tbhb_rules//:buildifier_test",
            },
        ),
    ],
)
def test_workspace_checks(
    decide: ModuleType, status: str, path: str, checks: set[str]
) -> None:
    assert decide.classify(f":100644 100644 0 0 {status}\0{path}\0") == (
        "",
        {"//:gazelle_test", *checks},
    )


def test_query_construction(decide: ModuleType) -> None:
    assert decide.classify("") == ("", set())
    narrow = "tests(//go:all)"
    query = decide.candidate_query('set("//a:manual_test")', narrow)
    assert query.endswith(f"intersect ({narrow})")
    assert "except set(//factory/cli:mutmut_test)" in query
    assert all(
        pattern.removeprefix("-") in query for pattern in decide.fallback().splitlines()
    )
    assert "intersect" not in decide.candidate_query("set()", "")
    assert (
        decide.label_set(["//a:with space", '//b:"', ""])
        == 'set("//a:with space" "//b:\\"")'
    )


def test_combination_and_rendering(decide: ModuleType) -> None:
    assert decide.combine(["@@//:x", "@r//:z", ""], ["//:x", "//:a"]) == [
        "//:a",
        "//:x",
        "@r//:z",
    ]
    reason = "\n\r\x7f" + string.punctuation
    assert decide.output_text(
        "0", decide.safe_reason(reason), "runner/targets", "", "EOF_token"
    ) == (
        f"all=false\ncount=0\nreason=???{string.punctuation}\nfile=runner/targets\n"
        "targets<<EOF_token\nEOF_token\n"
    )
    assert decide.summary_text("all", decide.safe_reason(reason)) == (
        "### Affected Bazel targets\n\n"
        "Running every target in the root and shared rules modules: \\?\\?\\?"
        + "".join(f"\\{char}" for char in string.punctuation)
        + "\n"
    )
    assert decide.stopped("##[notice]abc\n::error::text", "token") == (
        "::stop-commands::token\n  | ##[notice]abc\n  | ::error::text\n::token::\n"
    )


@pytest.mark.parametrize(
    ("raw", "readable"),
    [
        ("{}", True),
        ('{"results":[]}', True),
        ("{} {}", False),
        ("", False),
        ("[]", False),
        ('{"results":{}}', False),
        ('{"results":false}', False),
        ('{"results":[{}]}', False),
    ],
)
def test_tag_output_shape(decide: ModuleType, raw: str, readable: bool) -> None:
    assert decide.tags_safe(raw) is readable


@pytest.mark.parametrize("tag", ["manual", "review manual", "review, manual"])
def test_manual_tag_is_exact(decide: ModuleType, tag: str) -> None:
    rule = {
        "name": "//go:test",
        "attribute": [
            {"name": "size", "stringValue": "small"},
            {"name": "tags", "stringListValue": [tag]},
        ],
    }
    raw = json.dumps({"rule": rule}, indent=2)
    assert decide.manual_labels(raw + "\n" + '{"rule":{"attribute":[]}}') == (
        'set("//go:test")' if tag == "manual" else "set()"
    )
    assert decide.tags_safe(json.dumps({"results": [{"target": {"rule": rule}}]})) == (
        tag != "manual"
    )
    assert decide.summary_text("0", "done").endswith("Selected 0 test targets: done\n")


@pytest.mark.parametrize(
    "attributes", [{}, [{"name": "tags", "stringListValue": "review"}]]
)
def test_wrong_attribute_types_are_unreadable(
    decide: ModuleType, attributes: object
) -> None:
    raw = json.dumps({"results": [{"target": {"rule": {"attribute": attributes}}}]})
    assert not decide.tags_safe(raw)
