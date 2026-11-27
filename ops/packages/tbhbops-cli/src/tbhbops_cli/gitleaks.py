"""The private gitleaks config: its rules, its text and its check.

A port of the generator in `tbhbdev host bootstrap`, step 7 (tbhb-dev/tools,
src/tbhbdev/host.py). Every function here is pure. What the rules keep out of
a repository arrives in Facts, and the other modules gather it.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING
lazy from pathlib import PurePosixPath

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

VAULT = "host-0-shared"
ITEM = "scanner-policy"
FIELD = "config"
REF_SCHEME = "op://"
REF = f"{REF_SCHEME}{VAULT}/{ITEM}/{FIELD}"
EXTENDS = ".gitleaks.toml"
CANARY_ID = "tbhb-private-canary"
# The line tbhb_rules' gitleaks_tool.sh.tpl looks for.
CANARY = re.compile(
    rf"""^[ \t]*id[ \t]*=[ \t]*["']{CANARY_ID}["'][ \t]*(?:#.*)?$""",
    re.MULTILINE,
)
CANARY_REGEX = rf"\b{CANARY_ID}-[0-9a-f]{{8}}\b"
AGENT = "agent"
# A name shorter than NAME_MIN, or a common word, would match too much on its
# own: a repository's then matches only as owner/name, and other names get no
# rule. The words are the system dictionary, which lacks plurals and these terms.
NAME_MIN = 4
TECH_WORDS = frozenset({"config", "dotfiles", "github", "infra", "monorepo", "website"})
# Repository names that read as ordinary prose, though the dictionary lacks the
# word or the name joins several. They match only as owner/name, like a name
# every part of which is a common word.
ORDINARY_REPOS = frozenset(
    {
        "agent-tools",
        "design",
        "devtools",
        "drafts",
        "evals",
        "github-actions",
        "o11y",
        "ops",
        "pre-commit-hooks",
        "research",
        "rules",
        "runbook",
        "spikes",
        "tools",
    }
)
# Tailnet host names that share their first STEM_MIN characters give a stem,
# which matches the names to come as well.
STEM_MIN = 4
# The directories the tools write under .config, .cache and .local.
TOOL_DIRS = ("awrel", "gh-agent", "gitleaks", "tbhbdev")
# The names tbhbdev gives its keychain and its Connect service.
KEYCHAIN = "ops-connect.keychain"
SERVICE = "ops-connect"
# The rule categories, in the config's order, and each one's description. The
# rule ids are the category and a number, so no id holds a value.
CATEGORIES = {
    "private-repo": "A private repository's name",
    "tailnet-suffix": "The tailnet's MagicDNS suffix",
    "tailnet-name": "The tailnet's name",
    "tailnet-host": "A tailnet host name",
    "tailnet-stem": "The stem a family of tailnet host names shares",
    "tailnet-ipv4": "A tailnet IPv4 address",
    "tailnet-ipv6": "A tailnet IPv6 address",
    "home-path": "A path into the operator's or the agent's home",
    "checkout-path": "A path into the workspace checkouts",
    "tool-dir": "A path into a directory the tools write",
    "private-ipv4": "A private IPv4 address",
    "local-name": "A .local or .lan host name",
    "mac-address": "A MAC address",
    "op-reference": "A 1Password secret reference",
    "session-socket": "A session socket path",
    "account-name": "An operator's account name",
    "op-account": "The 1Password account",
    "op-vault": "A 1Password vault name",
    "op-item": "The name of a 1Password item the tools read",
    "endpoint": "A Connect endpoint's host name",
    "keychain": "A keychain, keyring or Connect server name",
}
# An IPv4 octet, and an address bounded as the committed public-ipv4 rule
# bounds one.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = r"(?:^|[^0-9A-Za-z.]){}(?:[^0-9.]|$)"
# What may come before a path's interesting part: anything but a space, a
# quote or the punctuation around a path.
_PATH = r"""[^\s"'`<>()\[\],;=]*"""
# A path that starts at $HOME or ~ names no one's home.
GENERIC_HOME = r"^(?:~|\$HOME|\$\{HOME\})/"


@dataclass(frozen=True)
class Tailnet:
    """The tailnet's MagicDNS suffix and name, and its hosts' names."""

    suffix: str
    name: str
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    """What the private gitleaks config keeps out of a repository."""

    repos: tuple[tuple[str, str], ...]
    tailnet: Tailnet
    words: frozenset[str]
    operators: tuple[str, ...]
    checkout: PurePosixPath | None
    vaults: tuple[str, ...]
    items: tuple[str, ...]
    endpoints: tuple[str, ...]
    names: tuple[str, ...]
    account: str
    session_sockets: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    category: str
    regex: str
    allow: tuple[str, ...] = ()


def problem(text: str) -> str | None:
    """Why TEXT isn't the private gitleaks config, or None. It never quotes TEXT."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"{REF} isn't valid TOML"
    extend = data.get("extend")
    if not isinstance(extend, dict) or extend.get("path") != EXTENDS:
        return f'{REF} has no [extend] path = "{EXTENDS}"'
    if not CANARY.search(text):
        return f"{REF} has no rule with the id {CANARY_ID}"
    return None


def _listed(word: str, words: frozenset[str]) -> bool:
    """Whether WORD, or its singular, is one of WORDS."""
    forms = {word, word.removesuffix("s"), word.removesuffix("es")}
    return not forms.isdisjoint(words)


def _common(name: str, words: frozenset[str]) -> bool:
    """Whether NAME is too short, or too common a word, to match alone."""
    word = name.lower().removeprefix(".")
    return len(word) < NAME_MIN or _listed(word, words)


def _ordinary(name: str, words: frozenset[str]) -> bool:
    """Whether a repository NAME is common, listed, or made only of common words."""
    word = name.lower().removeprefix(".")
    return (
        _common(name, words)
        or word in ORDINARY_REPOS
        or all(_listed(part, words) for part in re.split(r"[-_]", word))
    )


def _bound(text: str, at: int) -> str:
    """A word boundary, when TEXT[AT] is an ASCII word character as RE2 sees it."""
    return r"\b" if re.fullmatch(r"[A-Za-z0-9_]", text[at]) else ""


def _literal(value: str) -> str:
    """A case-blind regex for VALUE as a whole word."""
    return f"(?i){_bound(value, 0)}{re.escape(value)}{_bound(value, -1)}"


def _named(
    category: str, values: Iterable[str], words: frozenset[str]
) -> tuple[list[Rule], int]:
    """A rule for each of VALUES that isn't common, and how many were."""
    every = sorted(set(values))
    kept = [v for v in every if not _common(v, words)]
    return [Rule(category, _literal(v)) for v in kept], len(every) - len(kept)


def _repos(facts: Facts) -> tuple[list[Rule], int]:
    """A rule for each repository, which an ordinary name matches only scoped."""
    found: list[Rule] = []
    scoped_only = 0
    for owner, name in sorted(set(facts.repos)):
        scoped = rf"\b{re.escape(owner)}/"
        if _ordinary(name, facts.words):
            scoped_only += 1
        else:
            scoped = f"(?:{scoped}|{_bound(name, 0)})"
        found.append(
            Rule("private-repo", f"(?i){scoped}{re.escape(name)}{_bound(name, -1)}")
        )
    return found, scoped_only


def _shared(names: Sequence[str]) -> str:
    """The longest prefix all of NAMES have."""
    first, last = min(names), max(names)
    # LAST sorts after FIRST, so it differs from FIRST before it runs out.
    for at, mine in enumerate(first):
        if mine != last[at]:
            return first[:at]
    return first


def stems(hosts: Iterable[str]) -> list[str]:
    """The prefixes each family of host names sharing STEM_MIN characters has."""
    families: dict[str, list[str]] = {}
    for host in hosts:
        families.setdefault(host[:STEM_MIN], []).append(host)
    found = {
        re.sub(r"[-._0-9]+\Z", "", _shared(family))
        for family in families.values()
        if len(family) > 1
    }
    return sorted(found)


def _ipv4(category: str, *addresses: str) -> list[Rule]:
    return [Rule(category, _IPV4.format(a)) for a in addresses]


def rules(facts: Facts) -> tuple[list[Rule], dict[str, int]]:
    """The rules for FACTS, and how many values each category left out.

    A repository's name that is ordinary still matches as owner/name, and
    counts as left out of the bare match.
    """
    net = facts.tailnet
    made, left = _repos(facts)
    omitted = {"private-repo": left}
    made += [Rule("tailnet-suffix", _literal(net.suffix))]
    made += [Rule("tailnet-name", _literal(net.name))]
    found, omitted["tailnet-host"] = _named("tailnet-host", net.hosts, facts.words)
    made += found
    every = stems(net.hosts)
    kept = [s for s in every if not _common(s, facts.words)]
    omitted["tailnet-stem"] = len(every) - len(kept)
    made += [Rule("tailnet-stem", f"(?i){_bound(s, 0)}{re.escape(s)}") for s in kept]
    made += _ipv4(
        "tailnet-ipv4",
        rf"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.{_OCTET}){{2}}",
    )
    prefix = ":".join(("fd7a", "115c", "a1e0"))
    made += [Rule("tailnet-ipv6", rf"(?i)\b{prefix}:[0-9a-f:]*")]
    made += [
        Rule("home-path", f"/(?:Users|home)/{re.escape(name)}{_bound(name, -1)}")
        for name in sorted({*facts.operators, AGENT})
    ]
    if facts.checkout is not None:
        root = str(facts.checkout)
        regex = f"{_PATH}/{re.escape(root)}{_bound(root, -1)}"
        made += [Rule("checkout-path", regex, (GENERIC_HOME,))]
    tools = "|".join(map(re.escape, TOOL_DIRS))
    # A regex naming directories, not a path to a credential, which gitleaks
    # flags for its .config.
    dirs = r"/(?:\.config|\.cache|\.local/(?:share|state))/"  # gitleaks:allow
    regex = rf"{_PATH}{dirs}(?:{tools})\b"
    made += [Rule("tool-dir", regex, (GENERIC_HOME,))]
    made += _ipv4(
        "private-ipv4",
        rf"10(?:\.{_OCTET}){{3}}",
        rf"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.{_OCTET}){{2}}",
        rf"192\.168(?:\.{_OCTET}){{2}}",
    )
    made += [
        Rule("local-name", r"(?i)\b[a-z0-9][a-z0-9-]*\.(?:local|lan)(?:[^\w(.-]|$)"),
        Rule(
            "mac-address",
            r"(?i)\b[0-9a-f]{2}(?:(?::[0-9a-f]{2}){5}|(?:-[0-9a-f]{2}){5})\b",
        ),
        Rule("op-reference", REF_SCHEME + r"""[^\s"'`<>]+"""),
    ]
    made += [Rule("session-socket", re.escape(path)) for path in facts.session_sockets]
    found, omitted["account-name"] = _named(
        "account-name", facts.operators, facts.words
    )
    made += [*found, Rule("op-account", _literal(facts.account))]
    for category, values in (
        ("op-vault", facts.vaults),
        ("op-item", facts.items),
        ("endpoint", facts.endpoints),
        ("keychain", facts.names),
    ):
        found, omitted[category] = _named(category, values, facts.words)
        made += found
    return made, omitted


def _rule_lines(rule_id: str, description: str, regex: str) -> list[str]:
    return [
        "[[rules]]",
        f"id = {json.dumps(rule_id)}",
        f"description = {json.dumps(description)}",
        f"regex = {json.dumps(regex)}",
        "",
    ]


def render(made: Iterable[Rule]) -> str:
    """The config: the committed one, extended by the canary and MADE."""
    lines = [
        "# Generated by `tbhbops gitleaks generate`, which replaces any edit.",
        "",
        "[extend]",
        f"path = {json.dumps(EXTENDS)}",
        "",
        *_rule_lines(
            CANARY_ID, "The canary: the private config is in use", CANARY_REGEX
        ),
    ]
    counts: dict[str, int] = {}
    for rule in made:
        counts[rule.category] = counts.get(rule.category, 0) + 1
        rule_id = f"{rule.category}-{counts[rule.category]}"
        lines += _rule_lines(rule_id, CATEGORIES[rule.category], rule.regex)
        if rule.allow:
            lines += ["[[rules.allowlists]]", f"regexes = {json.dumps(rule.allow)}", ""]
    return "\n".join(lines)
