# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Decisions and output formats for affected Bazel targets."""

import fnmatch
import json
import os
import string
import unicodedata

PATTERNS = (
    "//...",
    "@tbhb_rules//...",
    *(
        f"@tbhb_rules_{name}//..."
        for name in [
            "ansible",
            "astro",
            "cloudflare",
            "d2",
            "go",
            "playwright",
            "python",
            "storybook",
            "structurizr",
            "swift",
            "tokens",
            "typescript",
        ]
    ),
)
EXCLUDED = (
    "@tbhb_rules//tools:mutmut_test",
    "@tbhb_rules_go//tests/self:gomutants_test",
    "@tbhb_rules_python//private:mutmut_test",
    "//factory/cli:mutmut_test",
)
SCOPE = "(" + "+".join(PATTERNS) + ")"
MANUAL = r"(^\[|, )manual(, |\]$)"
CONFIG = (
    "MODULE.bazel*",
    "*.MODULE.bazel",
    "WORKSPACE*",
    "REPO.bazel",
    ".bazelrc*",
    "*bazelrc",
    ".bazelversion",
    ".bazeliskrc",
    ".bazelignore",
)
BUILD = ("BUILD", "BUILD.*", "*.BUILD", "*.bazel", "*.bzl", "*.sky", "*.star")
GITLEAKS = (
    "lib/rules/.gitleaks*.toml",
    "lib/rules/*/.gitleaks*.toml",
    "lib/rules/lint/gitleaks/*",
    "lib/rules/pins/extensions.bzl",
    "LICENSES/MIT.txt",
)


def initial_reason(event, head, want, query):
    if not want:
        return f"no event head to check the checkout {head} against"
    if event not in {"pull_request", "merge_group", "workflow_dispatch"}:
        return f"a '{event}' event tests everything"
    if head != want:
        return f"the checkout {head} is not the event's head {want}"
    return "the query is not one line" if "\n" in query else ""


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def classify(raw):
    checks = {"//:gazelle_test"} if raw else set()
    fields = iter(raw.split("\0"))
    for meta, path in zip(fields, fields):
        modes = meta[1:14].split()
        if "160000" in modes:
            return f"the submodule {path} changed", set()
        if "120000" in modes:
            return f"the symlink {path} changed", set()
        name = path.rsplit("/", 1)[-1]
        if matches(name, CONFIG):
            return f"{path} changed", set()
        rules = path.startswith("lib/rules/")
        if matches(name, BUILD):
            checks.add(
                "@tbhb_rules//:buildifier_test" if rules else "//:buildifier_test"
            )
        if rules and path.endswith((".sh", ".sh.tpl", ".yml")):
            checks.add("@tbhb_rules//repo/tests:mktemp_test")
        if rules and (
            not meta.endswith("M")
            or path.endswith(".md")
            or path == "lib/rules/.markdown-links.toml"
        ):
            checks.add("@tbhb_rules//repo/tests:markdown_links_test")
        if matches(path, GITLEAKS):
            checks.add("@tbhb_rules//lint/tests:gitleaks_configs_test")
    return "", checks


def fallback():
    return lines((*PATTERNS, *(f"-{label}" for label in EXCLUDED)))


def lines(labels):
    return "".join(label + "\n" for label in labels)


def label_set(labels):
    return (
        "set("
        + " ".join(json.dumps(label, ensure_ascii=False) for label in labels if label)
        + ")"
    )


def candidate_query(manual, query):
    # Non-manual suites can still explicitly include manual tests.
    result = (
        f"let suites = kind('test_suite rule', {SCOPE}) in "
        f"((kind('.*_test rule', {SCOPE}) except {manual}) + tests($suites except {manual})) "
        f"except set({' '.join(EXCLUDED[:-1])}) except set({EXCLUDED[-1]})"
    )
    return f"({result}) intersect ({query})" if query else result


def manual_tag(rule):
    attributes = rule["attribute"]
    if not isinstance(attributes, list):
        raise TypeError("the rule's attributes must be a list")
    for attribute in attributes:
        if attribute["name"] == "tags":
            tags = attribute["stringListValue"]
            if not isinstance(tags, list):
                raise TypeError("the rule's tags must be a list")
            if "manual" in tags:
                return True
    return False


def manual_labels(raw):
    decoder = json.JSONDecoder()
    rules = []
    while raw := raw.lstrip():
        item, end = decoder.raw_decode(raw)
        rules.append(item["rule"])
        raw = raw[end:]
    return label_set(rule["name"] for rule in rules if manual_tag(rule))


def tags_safe(raw):
    try:
        results = json.loads(raw).get("results")
        return results is None or (
            isinstance(results, list)
            and not any(manual_tag(hit["target"]["rule"]) for hit in results)
        )
    except ValueError, AttributeError, KeyError, TypeError:
        return False


def combine(affected, checks):
    return sorted(
        {
            label.removeprefix("@@") if label.startswith("@@//") else label
            for label in (*affected, *checks)
            if label
        },
        key=os.fsencode,
    )


def safe_reason(reason):
    return "".join(
        "?" if unicodedata.category(char) in {"Cc", "Cs"} else char for char in reason
    )


def output_text(count, reason, path, targets, delimiter):
    return (
        f"all={str(count == 'all').lower()}\ncount={count}\nreason={reason}\nfile={path}\n"
        f"targets<<{delimiter}\n{targets}{delimiter}\n"
    )


def summary_text(count, reason):
    line = (
        "Running every target in the root and shared rules modules"
        if count == "all"
        else f"Selected {count} test targets"
    )
    escaped = reason.translate({ord(char): f"\\{char}" for char in string.punctuation})
    return f"### Affected Bazel targets\n\n{line}: {escaped}\n"


def stopped(message, token):
    body = lines(f"  | {line}" for line in message.splitlines())
    return f"::stop-commands::{token}\n{body}::{token}::\n"
