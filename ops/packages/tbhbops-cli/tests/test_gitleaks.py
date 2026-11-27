"""Tests for the rules, the rendered config and its check."""

import re
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from tbhbops_cli import gitleaks

GOLDEN = Path(__file__).with_name("golden.txt")
WORDS = frozenset({"alpha", "common", "shared", "config", "agent", "box"})
REPOS = (
    ("ex-org", "secret-thing"),
    ("ex-org", "config"),
    ("ex-org", "ab"),
    ("ex-user", "Zeta.Repo"),
    ("ex-org", "dots"),
)
HOSTS = ("ex-lab1", "ex-lab2", "ex-lab3", "ex-edge", "solo", "alpha", "ex-lab-9")


def facts(**changes: object) -> gitleaks.Facts:
    base = {
        "repos": REPOS,
        "tailnet": gitleaks.Tailnet("tail-ex.net", "ex.example", HOSTS),
        "words": WORDS,
        "operators": ("opr", "tony", "alpha"),
        "checkout": PurePosixPath("Code/ex"),
        "vaults": ("all", "host-x", "ab"),
        "items": ("scanner-policy",),
        "endpoints": ("op-connect.tail-ex.net",),
        "names": ("k.chain", "svc", "srv"),
        "account": "acct.example.com",
        "session_sockets": ("/tmp/sock/a.sock", "/var/x y"),  # noqa: S108  # never opened
    }
    return gitleaks.Facts(**(base | changes))  # pyrefly: ignore


def test_the_config_is_the_one_tbhbdev_host_bootstrap_makes() -> None:
    # golden.txt is what bootstrap's own generator rendered for these facts,
    # apart from the header line that names the command.
    made, _ = gitleaks.rules(facts())
    assert gitleaks.render(made) == (
        GOLDEN.read_text(encoding="utf-8")
        .replace("{slash}", "/")
        .replace("{overlay_prefix}", ":".join(("fd7a", "115c", "a1e0")))
    )


def test_the_rules_count_what_each_category_left_out() -> None:
    _, omitted = gitleaks.rules(facts())
    assert omitted == {
        "private-repo": 2,
        "tailnet-host": 1,
        "tailnet-stem": 0,
        "account-name": 2,
        "op-vault": 2,
        "op-item": 0,
        "endpoint": 0,
        "keychain": 2,
    }


def test_without_a_checkout_or_sockets_those_categories_have_no_rule() -> None:
    made, _ = gitleaks.rules(facts(checkout=None, session_sockets=()))
    categories = {rule.category for rule in made}
    assert "checkout-path" not in categories
    assert "session-socket" not in categories
    assert categories < set(gitleaks.CATEGORIES)


def test_every_category_has_a_rule_when_every_fact_is_present() -> None:
    made, _ = gitleaks.rules(facts())
    assert {rule.category for rule in made} == set(gitleaks.CATEGORIES)


def test_the_rendered_config_passes_its_own_check_and_parses() -> None:
    made, _ = gitleaks.rules(facts())
    text = gitleaks.render(made)
    assert gitleaks.problem(text) is None
    parsed = tomllib.loads(text)
    assert parsed["extend"] == {"path": ".gitleaks.toml"}
    assert parsed["rules"][0]["id"] == "tbhb-private-canary"
    assert len(parsed["rules"]) == len(made) + 1


def test_the_rule_ids_are_the_category_and_a_number() -> None:
    made, _ = gitleaks.rules(facts())
    parsed = tomllib.loads(gitleaks.render(made))
    ids = [rule["id"] for rule in parsed["rules"]]
    assert ids[:3] == ["tbhb-private-canary", "private-repo-1", "private-repo-2"]
    assert "tailnet-host-6" in ids
    assert "tailnet-host-7" not in ids
    assert all(re.fullmatch(r"[a-z0-9-]+-[0-9]+", i) for i in ids[1:])


def test_a_value_never_appears_in_a_rule_id_or_a_description() -> None:
    made, _ = gitleaks.rules(facts())
    parsed = tomllib.loads(gitleaks.render(made))
    labels = " ".join(f"{r['id']} {r['description']}" for r in parsed["rules"])
    assert "secret-thing" not in labels
    assert "tail-ex" not in labels


def test_the_canary_matches_only_its_own_id_line() -> None:
    assert gitleaks.CANARY.search('id = "tbhb-private-canary"')
    assert gitleaks.CANARY.search("  id=\t'tbhb-private-canary'  # why")
    assert not gitleaks.CANARY.search('id = "tbhb-private-canary-2"')
    assert not gitleaks.CANARY.search('xid = "tbhb-private-canary"')
    assert not gitleaks.CANARY.search('description = "id = tbhb-private-canary"')
    assert not gitleaks.CANARY.search('id = "tbhb-private-canary" x')


def test_the_canary_regex_matches_a_canary_and_no_other_word() -> None:
    regex = re.compile(gitleaks.CANARY_REGEX)
    fixture = f"tbhb-private-canary-{int('0123abcd', 16):08x}"
    assert regex.search(f"a {fixture}.")
    assert not regex.search("tbhb-private-canary-0123abc")
    assert not regex.search("tbhb-private-canary-0123abcdef")
    assert not regex.search("tbhb-private-canary-0123ABCD")


def test_the_reference_names_the_field_in_the_all_vault() -> None:
    assert gitleaks.REF == "op:\x2f/host-0-shared/scanner-policy/config"


def test_text_that_is_not_toml_is_a_problem_that_does_not_quote_it() -> None:
    found = gitleaks.problem("secret = = value")
    assert found == "op:\x2f/host-0-shared/scanner-policy/config isn't valid TOML"


@pytest.mark.parametrize(
    "text",
    [
        'id = "tbhb-private-canary"\n',
        '[extend]\npath = "other.toml"\n[[rules]]\nid = "tbhb-private-canary"\n',
        '[extend]\n[[rules]]\nid = "tbhb-private-canary"\n',
        'extend = "x"\n[[rules]]\nid = "tbhb-private-canary"\n',
    ],
)
def test_text_that_does_not_extend_the_committed_config_is_a_problem(text: str) -> None:
    assert gitleaks.problem(text) == (
        'op:\x2f/host-0-shared/scanner-policy/config has no [extend] path = ".gitleaks.toml"'
    )


def test_text_without_the_canary_is_a_problem() -> None:
    text = '[extend]\npath = ".gitleaks.toml"\n[[rules]]\nid = "other"\n'
    assert gitleaks.problem(text) == (
        "op:\x2f/host-0-shared/scanner-policy/config has no rule with the id "
        "tbhb-private-canary"
    )


@pytest.mark.parametrize(
    ("name", "common"),
    [
        ("abc", True),
        ("abcd", False),
        ("Alpha", True),
        ("alphas", True),
        ("boxes", True),
        (".alpha", True),
        ("ALPHAS", True),
        ("zebra", False),
        (".abc", True),
        (".abcd", False),
    ],
)
def test_a_short_or_common_name_is_common(name: str, common: bool) -> None:
    assert gitleaks._common(name, WORDS) is common  # noqa: SLF001


@pytest.mark.parametrize(
    ("text", "at", "bound"),
    [
        ("abc", 0, r"\b"),
        ("abc", -1, r"\b"),
        ("_a", 0, r"\b"),
        ("9a", 0, r"\b"),
        (".ab", 0, ""),
        ("ab.", -1, ""),
        ("-ab", 0, ""),
        ("é", 0, ""),
    ],
)
def test_a_boundary_follows_ascii_word_characters_only(
    text: str, at: int, bound: str
) -> None:
    assert gitleaks._bound(text, at) == bound  # noqa: SLF001


def test_a_literal_is_a_case_blind_whole_word_with_escapes() -> None:
    assert gitleaks._literal("a.b") == r"(?i)\ba\.b\b"  # noqa: SLF001
    assert gitleaks._literal(".a.") == r"(?i)\.a\."  # noqa: SLF001
    assert gitleaks._literal("-a-") == r"(?i)\-a\-"  # noqa: SLF001


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [
        (["moon1", "moon2", "moon3"], ["moon"]),
        (["lab1", "lab2", "lab3"], []),
        (["lab-1", "lab-22"], ["lab"]),
        (["lab_1.x", "lab_2.x"], ["lab"]),
        (["abcd", "abcdef"], ["abcd"]),
        (["moon1", "moon2", "star1", "star2"], ["moon", "star"]),
        (["solo", "other"], []),
        (["abcd-1", "abcd-1"], ["abcd"]),
        (["abc1", "abc2"], []),
        ([], []),
    ],
)
def test_a_family_of_names_gives_its_shared_prefix_as_a_stem(
    hosts: list[str], expected: list[str]
) -> None:
    assert gitleaks.stems(hosts) == expected


def test_a_family_whose_names_are_all_equal_keeps_one_stem() -> None:
    assert gitleaks.stems(["abcd", "abcd"]) == ["abcd"]
    assert gitleaks._shared(["abc"]) == "abc"  # noqa: SLF001
    assert gitleaks._shared(["moon", "moss", "mop"]) == "mo"  # noqa: SLF001


def test_a_common_repository_name_matches_only_scoped_and_others_either_way() -> None:
    made, left = gitleaks._repos(facts())  # noqa: SLF001
    assert left == 2
    by_name = {rule.regex: rule for rule in made}
    scoped = r"(?i)\bex\-org/config\b"
    assert scoped in by_name
    both = r"(?i)(?:\bex\-org/|\b)secret\-thing\b"
    assert both in by_name
    for pattern, hit, miss in (
        (scoped, "ex-org/config", "config"),
        (both, "secret-thing", "ex-org/secrets-thing"),
    ):
        assert re.search(pattern, f"see {hit} here")
        assert not re.search(pattern, f"see {miss} here")


ORDINARY_WORDS = frozenset({"agent", "tools", "pre", "commit", "hooks", "my"})


def repo_regex(
    name: str, words: frozenset[str] = frozenset(), owner: str = "tbhb-dev"
) -> str:
    """The one rule that `_repos` makes for tbhb-dev/NAME."""
    made, _ = gitleaks._repos(facts(repos=((owner, name),), words=words))  # noqa: SLF001
    (rule,) = made
    return rule.regex


def test_a_listed_repository_name_matches_only_with_its_owner() -> None:
    # "runbook" is no dictionary word, so the allowlist keeps it from matching bare.
    regex = repo_regex("runbook", owner="example-org")
    assert re.search(regex, "see example-org/runbook for it")
    assert re.search(regex, "see EXAMPLE-ORG/Runbook for it")
    assert not re.search(regex, "see your ops runbook")
    assert not re.search(regex, "runbook")


@pytest.mark.parametrize(
    "name",
    [
        "runbook",
        "devtools",
        "o11y",
        "ops",
        "drafts",
        "evals",
        "research",
        "spikes",
        "rules",
        "tools",
        "design",
        "agent-tools",
        "github-actions",
        "pre-commit-hooks",
        "Runbook",
        ".Runbook",
    ],
)
def test_the_known_ordinary_repository_names_match_only_with_their_owner(
    name: str,
) -> None:
    assert gitleaks._ordinary(name, frozenset()) is True  # noqa: SLF001
    assert not re.search(repo_regex(name), f"the {name.lstrip('.')} here")


@pytest.mark.parametrize(
    ("name", "ordinary"),
    [
        ("agent-tools", True),
        ("Agent_Tools", True),
        (".agent-tools", True),
        ("my-agent-tools", True),
        ("agent-widget", False),
        ("agent--tools", False),
        ("-agent", False),
        ("zebra", False),
        ("quokka", False),
    ],
)
def test_a_name_made_only_of_common_words_is_ordinary(
    name: str, *, ordinary: bool
) -> None:
    assert gitleaks._ordinary(name, ORDINARY_WORDS) is ordinary  # noqa: SLF001


def test_a_name_of_common_words_joined_by_hyphens_matches_only_with_its_owner() -> None:
    regex = repo_regex("agent-widget", ORDINARY_WORDS | {"widget"})
    assert re.search(regex, "see tbhb-dev/agent-widget")
    assert not re.search(regex, "an agent-widget here")
    regex = repo_regex("agent-tools", ORDINARY_WORDS, owner="example-org")
    assert re.search(regex, "see example-org/agent-tools")
    assert not re.search(regex, "the agent-tools here")


def test_a_distinctive_repository_name_still_matches_bare() -> None:
    for name in ("zebra", "quokka"):
        regex = repo_regex(name, ORDINARY_WORDS)
        assert re.search(regex, f"see {name} here")
        assert re.search(regex, f"see tbhb-dev/{name} here")
        assert not re.search(regex, f"see {name}s here")


def test_rules_count_the_ordinary_names_among_the_repositories() -> None:
    repos = (
        ("tbhb-dev", "runbook"),
        ("tbhb-dev", "agent-tools"),
        ("tbhb-dev", "zebra"),
    )
    made, omitted = gitleaks.rules(facts(repos=repos, words=ORDINARY_WORDS))
    assert omitted["private-repo"] == 2
    assert sum(r.category == "private-repo" for r in made) == 3


def quad(*octets: int) -> str:
    """A dotted quad built from parts, so no address sits in the source."""
    return ".".join(map(str, octets))


def test_the_rules_match_what_they_are_for() -> None:
    made, _ = gitleaks.rules(facts())
    by_category: dict[str, list[str]] = {}
    for rule in made:
        by_category.setdefault(rule.category, []).append(rule.regex)

    def hits(category: str, text: str) -> bool:
        return any(re.search(r, text) for r in by_category[category])

    assert hits("tailnet-ipv4", f"x {quad(100, 64, 0, 1)} y")
    assert hits("tailnet-ipv4", quad(100, 127, 255, 254))
    assert not hits("tailnet-ipv4", quad(100, 128, 0, 1))
    assert not hits("tailnet-ipv4", quad(100, 63, 0, 1))
    assert hits("tailnet-ipv6", ":".join(("fd7a", "115c", "a1e0", "ab12", "", "1")))
    assert hits("private-ipv4", quad(10, 1, 2, 3))
    assert hits("private-ipv4", quad(172, 16, 0, 1))
    assert hits("private-ipv4", quad(172, 31, 255, 1))
    assert hits("private-ipv4", quad(192, 168, 1, 1))
    assert not hits("private-ipv4", quad(172, 32, 0, 1))
    assert not hits("private-ipv4", quad(11, 1, 2, 3))
    assert hits("local-name", ".".join(("box", "local")))
    assert hits("mac-address", ":".join(['02', '00', '5e', '00', '53', '07']))
    assert hits("mac-address", "-".join(['02', '00', '5E', '00', '53', '04']))
    assert hits("op-reference", "op:\x2f/vault/item/field")
    assert hits("home-path", "/Users/opr/x")
    assert hits("home-path", "/".join(("/home", gitleaks.AGENT, "x")))
    assert not hits("home-path", "/Users/oprah/x")
    assert hits("tool-dir", "/".join(("/abc", ".config", "awrel")))
    assert hits("tool-dir", "/".join(("/abc", ".local", "share", "tbhbdev", "x")))
    assert hits("checkout-path", "/Users/x/Code/ex/y")
    assert hits("tailnet-suffix", "box.TAIL-EX.NET")
    assert hits("op-account", "ACCT.example.com")
    assert hits("session-socket", "at /tmp/sock/a.sock now")
    assert hits("tailnet-stem", "EX-LAB77")


def test_a_name_that_ends_or_starts_in_punctuation_gets_no_boundary_there() -> None:
    made, _ = gitleaks.rules(
        facts(
            repos=(("o", "a.bcd"), ("o", "abcd.")),
            tailnet=gitleaks.Tailnet("tail-ex.net", "ex.example", ("a.bc1", "a.bc2")),
            operators=("abc.",),
            checkout=PurePosixPath("Code/ex."),
        )
    )

    def regexes(category: str) -> list[str]:
        return [r.regex for r in made if r.category == category]

    assert regexes("private-repo") == [
        r"(?i)(?:\bo/|\b)a\.bcd\b",
        r"(?i)(?:\bo/|\b)abcd\.",
    ]
    assert regexes("tailnet-stem") == [r"(?i)\ba\.bc"]
    assert regexes("home-path") == [
        r"/(?:Users|home)/abc\.",
        r"/(?:Users|home)/agent\b",
    ]
    (checkout,) = regexes("checkout-path")
    assert checkout.endswith(r"/Code/ex\.")
