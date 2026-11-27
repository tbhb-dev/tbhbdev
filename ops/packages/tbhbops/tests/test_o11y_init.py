# cspell:ignore rustfsadmin rustfsadmi
"""Preparation verdicts, including the five source init.bats scenarios."""

import ast
from dataclasses import replace
from pathlib import Path
lazy from types import ModuleType

import pytest

from tbhbops.o11y import init, render, validate

FRESH = init.Facts(
    ("missing",) * 4, (False,) * 4, webhook_empty=True, smtp_exists=False, leftovers=()
)


@pytest.mark.parametrize(
    ("value", "charset", "length", "expected"),
    [
        ("ABC01", "ABC01", 5, True),
        ("ABC01", "ABC01", 4, False),
        ("ABC01", "ABC01", 6, False),
        ("ABC0!", "ABC01", 5, False),
        ("rustfsadmin", "rustfsadmin", 11, False),
        ("rustfsadmiN", "rustfsadmiN", 11, True),
    ],
)
def test_valid(value: str, charset: str, length: int, expected: bool) -> None:
    assert init.valid(value, charset, length) is expected


@pytest.mark.parametrize(
    ("facts", "generated", "refusal"),
    [
        pytest.param(FRESH, 4, None, id="bats-1-generate-before-store"),
        pytest.param(
            replace(FRESH, statuses=("ok",) * 4), 0, None, id="bats-1-preserve-on-rerun"
        ),
        pytest.param(
            replace(FRESH, guards=(False, False, True, True)),
            0,
            "grafana_admin_password",
            id="bats-2-later-guard-before-earlier-repair",
        ),
        pytest.param(
            replace(
                FRESH,
                statuses=("invalid", "ok", "ok", "ok"),
                guards=(True, True, False, False),
            ),
            0,
            "rustfs.access_key",
            id="bats-3-damaged-guarded-secret",
        ),
        pytest.param(
            replace(
                FRESH,
                statuses=("ok", "ok", "missing", "ok"),
                guards=(True, True, False, False),
            ),
            1,
            None,
            id="bats-4-add-independent-missing-file",
        ),
        pytest.param(
            replace(
                FRESH,
                statuses=("missing", "ok", "invalid", "ok"),
                guards=(False, False, True, True),
            ),
            0,
            "grafana_admin_password",
            id="bats-5-damage-before-pending-repair",
        ),
        pytest.param(
            replace(FRESH, statuses=("invalid",) * 4),
            4,
            None,
            id="invalid-before-store",
        ),
        pytest.param(
            replace(FRESH, statuses=("unreadable", "ok", "ok", "ok")),
            0,
            "can't read rustfs.access_key without root; plan incomplete",
            id="unreadable",
        ),
    ],
)
def test_plan_cases(facts: init.Facts, generated: int, refusal: str | None) -> None:
    result = init.plan(facts, (12, 34))
    if refusal is not None:
        assert isinstance(result, init.Refusal)
        assert refusal in result.message
    else:
        assert isinstance(result, init.Plan)
        assert (
            sum(isinstance(s, init.GenerateSecret) for s in result.steps) == generated
        )
        assert init.WriteS3Env(12, 34) in result.steps


def test_exact_fresh_plan_and_existing_placeholders() -> None:
    tail = tuple(
        init.SetOwnerMode(f"secrets/{s.name}", s.uid, s.gid, s.mode)
        for s in init.SECRETS
    )
    tail += tuple(
        init.SetOwnerMode(f"secrets/{n}", 472, 472, 0o400)
        for n in (init.WEBHOOK, init.SMTP)
    )
    assert init.plan(
        replace(FRESH, leftovers=("secrets/.tmp.old",)), (0, 0)
    ) == init.Plan(
        (
            *init.DIRECTORIES,
            init.RemoveLeftover("secrets/.tmp.old"),
            *(init.GenerateSecret(s) for s in init.SECRETS),
            init.WritePlaceholder(init.WEBHOOK),
            init.WritePlaceholder(init.SMTP),
            init.WriteS3Env(0, 0),
            *tail,
        )
    )
    result = init.plan(replace(FRESH, webhook_empty=False, smtp_exists=True), (0, 0))
    assert isinstance(result, init.Plan)
    assert not any(isinstance(s, init.WritePlaceholder) for s in result.steps)


@pytest.mark.parametrize("secret", [s for s in init.SECRETS if s.guard is not None])
def test_guard_refusal_names_secret_and_guard(secret: init.Secret) -> None:
    i = init.SECRETS.index(secret)
    statuses: list[init.Status] = ["ok"] * 4
    statuses[i] = "missing"
    result = init.plan(
        replace(FRESH, statuses=tuple(statuses), guards=(True,) * 4), (0, 0)
    )
    assert result == init.Refusal(
        f"{secret.name} is missing or damaged and {secret.guard} already exists; "
        "restore the file, nothing was changed"
    )


@pytest.mark.parametrize(
    "path",
    [
        "data",
        *(d.name for d in init.DIRECTORIES),
        *(s.guard for s in init.SECRETS if s.guard is not None),
        "secrets/.tmp.old",
        "secrets/s3.env",
        *(f"secrets/{s.name}" for s in init.SECRETS),
        "secrets/alert_smtp_password",
        "secrets/alert_discord_webhook_url",
    ],
)
def test_symlink_refusal(path: str) -> None:
    assert init.plan(replace(FRESH, symlinks=(path, "later")), (0, 0)) == init.Refusal(
        f"refusing symlink {path!r}; nothing was changed"
    )


@pytest.mark.parametrize("module", [init, render, validate])
def test_library_imports_have_no_io(module: ModuleType) -> None:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    # mutmut injects this one instrumentation import into the tested module.
    allowed = {
        "dataclasses",
        "string",
        "typing",
        "hashlib",
        "re",
        "collections.abc",
        "collections",
        "json",
        "mutmut.mutation.trampoline",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(n.name in allowed for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        elif isinstance(node, ast.Name):
            assert node.id != "open"


@pytest.mark.parametrize(
    ("statuses", "guards"),
    [
        (("ok",) * 2, (False,) * 4),
        (("ok",) * 5, (False,) * 4),
        (("ok",) * 4, (False,) * 2),
        (("ok",) * 4, (False,) * 5),
    ],
)
def test_inconsistent_facts_are_programming_errors(
    statuses: tuple[init.Status, ...], guards: tuple[bool, ...]
) -> None:
    with pytest.raises(ValueError, match="zip"):
        init.plan(replace(FRESH, statuses=statuses, guards=guards), (0, 0))


@pytest.mark.parametrize(("status", "invalid"), [("missing", False), ("invalid", True)])
def test_generation_records_reason(status: init.Status, *, invalid: bool) -> None:
    result = init.plan(replace(FRESH, statuses=(status, "ok", "ok", "ok")), (0, 0))
    assert isinstance(result, init.Plan)
    assert [s for s in result.steps if isinstance(s, init.GenerateSecret)] == [
        init.GenerateSecret(init.SECRETS[0], invalid=invalid)
    ]


def test_unreadable_is_incomplete() -> None:
    result = init.plan(
        replace(FRESH, statuses=("ok", "unreadable", "ok", "ok")), (0, 0)
    )
    assert result == init.Incomplete(
        "can't read rustfs.secret_key without root; plan incomplete"
    )


def test_agents_password_spec() -> None:
    assert init.SECRETS[-1] == replace(
        init.SECRETS[2], name="grafana_agents_password", guard=None
    )
    assert len(init.SECRETS) == 4
