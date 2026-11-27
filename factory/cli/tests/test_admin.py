"""CLI parsing and selected-secret repository workflow with stubbed calls."""

import io
import json
import sys
from dataclasses import replace
lazy from pathlib import Path

import httpx2
import pytest
from test_admin_request import fake as admin_fake
lazy from test_admin_request import Fake

from tbhbdev.admin import command as admin
from tbhbdev.admin.audit import RefusedError
from tbhbdev.alpha import gh_app_args
lazy from tbhbdev.admin import request

fake = admin_fake
RULESET = "/repos/tbhb-dev/tbhbdev/rulesets/28092610"
UPDATE_RULESET = ["call", "PUT", RULESET, "--reason", "test", "--json", "-"]


@pytest.fixture
def ruleset_update() -> tuple[dict[str, object], dict[str, object]]:
    thread_resolution = "required_review_thread_resolution"
    body: dict[str, object] = {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {},
        "bypass_actors": [],
        "rules": [{"type": "pull_request", "parameters": {thread_resolution: False}}],
    }
    current = {
        **body,
        "source": "tbhb-dev/tbhbdev",
        "source_type": "Repository",
        "id": 28092610,
        "rules": [{"type": "pull_request", "parameters": {thread_resolution: True}}],
    }
    return current, body


def ruleset_writes(fake: Fake) -> list[dict[str, object]]:
    audit = fake.runtime.audit.directory / "calls.jsonl"
    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    return [row for row in entries if row["method"] == "PUT"]


def test_approved_ruleset_call(
    fake: Fake,
    ruleset_update: tuple[dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, body = ruleset_update
    fake.by_method["GET"] = httpx2.Response(200, json=current)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    assert admin.main(UPDATE_RULESET, "admin", runtime=fake.runtime) == 0
    calls = [req for req in fake.requests if req.url.path == RULESET]
    assert [req.method for req in calls] == ["GET", "PUT"]
    assert json.loads(fake.requests[1].content)["permissions"] == {
        "administration": "write"
    }
    assert json.loads(calls[-1].content) == body
    writes = ruleset_writes(fake)
    assert [row["phase"] for row in writes] == ["before", "after"]
    for row in writes:
        assert row["decision"] == "allow"
        assert row["approval_decisions"] == [
            "2026-11-27-merge-does-not-require-resolved-threads",
        ]


@pytest.mark.parametrize(
    "attempt", ["refuse", "dry-run", "empty", "unreadable", "redacted", "http", "call"]
)
def test_ruleset_call_refusal(
    fake: Fake,
    ruleset_update: tuple[dict[str, object], dict[str, object]],
    attempt: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current, body = ruleset_update
    if attempt == "redacted":
        current["name"] = "eyJ.sample.value"
        body["name"] = "[redacted]"
    fake.by_method["GET"] = (
        httpx2.Response(200, text="malformed")
        if attempt == "unreadable"
        else httpx2.Response(404 if attempt == "http" else 200, json=current)
    )
    fake.failure = "call" if attempt == "call" else ""
    args = UPDATE_RULESET.copy()
    if attempt == "refuse":
        body["name"] = "other"
    if attempt == "empty":

        def missing_approval_directory(_package: str) -> Path:
            return fake.runtime.marker.parent

        monkeypatch.setattr(request, "files", missing_approval_directory)
    if attempt == "dry-run":
        args.append("--dry-run")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    assert admin.main(args, "admin", runtime=fake.runtime) == 3
    assert capsys.readouterr().err == "protection_weakening\n"
    methods = [req.method for req in fake.requests if req.url.path == RULESET]
    assert methods == ([] if attempt in {"empty", "dry-run"} else ["GET"])
    assert [row["decision"] for row in ruleset_writes(fake)] == ["refuse"]


@pytest.mark.parametrize(
    ("argv", "status", "output"),
    [
        (["--help"], 0, "out"),
        ([], 2, "err"),
        (["call", "GET", "/repos/tbhb-dev/widget"], 2, "err"),
        (["secret-repos", "remove"], 2, "err"),
    ],
)
def test_usage(
    argv: list[str], status: int, output: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.main(argv, "admin") == status
    captured = capsys.readouterr()
    assert getattr(captured, output) == admin.USAGE.format(prog="admin")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--reason", " "], "reason_required"),
        (["--reason", "test", "--vault", "../vault"], "invalid_setting"),
    ],
)
def test_invalid_options(
    fake: Fake, args: list[str], code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        admin.main(
            ["call", "GET", "/repos/tbhb-dev/widget", *args],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", f"{code}\n")
    assert fake.reads == fake.requests == []


def test_default_runtime_dispatch(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = replace(fake.runtime, settings=request.Settings())
    monkeypatch.setattr(admin, "default_runtime", lambda: runtime)
    fake.reply = httpx2.Response(404, text="missing")
    assert (
        admin.main(
            ["call", "GET", "/repos/tbhb-dev/widget", "--reason", "test"], "admin"
        )
        == 1
    )
    assert fake.reads == ["op://vault-admin/item-admin/private_key"]


@pytest.mark.parametrize(
    ("config", "flags", "reference"),
    [
        (True, ["--vault", "vault-override"], "vault-override/item-admin"),
        (True, ["--item", "item-override"], "vault-admin/item-override"),
        (
            False,
            ["--vault", "vault-admin", "--item", "item-admin"],
            "vault-admin/item-admin",
        ),
    ],
)
def test_credential_flags(
    fake: Fake, config_file: Path, config: bool, flags: list[str], reference: str
) -> None:
    if not config:
        config_file.unlink()
    assert (
        admin.main(
            ["call", "GET", "/repos/tbhb-dev/widget", "--reason", "test", *flags],
            "admin",
            runtime=fake.runtime,
        )
        == 0
    )
    assert fake.reads == [f"op://{reference}/private_key"]


@pytest.mark.parametrize(
    ("flags", "missing"), [([], "vault"), (["--vault", "vault-admin"], "item")]
)
def test_missing_credential_settings(
    fake: Fake,
    config_file: Path,
    flags: list[str],
    missing: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_file.unlink()
    assert (
        admin.main(
            ["call", "GET", "/repos/tbhb-dev/widget", "--reason", "test", *flags],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert capsys.readouterr() == (
        "",
        f"Set --{missing} or [admin] {missing} in {config_file}.\n",
    )
    assert fake.stores == fake.reads == fake.requests == []


@pytest.mark.parametrize("source", ["-", "file", "missing", "invalid"])
def test_body_input(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "body.json"
    if source == "-":
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"example": true}'))
        assert admin.body_from("-") == {"example": True}
    elif source == "file":
        path.write_text('{"example": true}')
        assert admin.body_from(str(path)) == {"example": True}
    else:
        if source == "invalid":
            path.write_text("invalid")
        with pytest.raises(RefusedError, match=r"^invalid_body$"):
            admin.body_from(str(path))


def selected_transport(
    fake: Fake, replies: dict[str, httpx2.Response]
) -> request.Runtime:
    original = fake.runtime.env.transport
    assert original is not None

    def transport(req: httpx2.Request) -> httpx2.Response:
        if req.url.path in replies:
            fake.reply = replies[req.url.path]
        return original.handle_request(req)

    return replace(
        fake.runtime,
        env=replace(fake.runtime.env, transport=httpx2.MockTransport(transport)),
    )


def test_secret_repos_add(fake: Fake) -> None:
    runtime = selected_transport(
        fake,
        {
            "/orgs/tbhb-dev/actions/secrets/build": httpx2.Response(
                200, json={"visibility": "selected"}
            ),
            "/repos/tbhb-dev/widget": httpx2.Response(200, json={"id": 1}),
            "/repos/tbhb-dev/other": httpx2.Response(200, json={"id": 9}),
            "/orgs/tbhb-dev/actions/secrets/build/repositories/1": httpx2.Response(204),
            "/orgs/tbhb-dev/actions/secrets/build/repositories/9": httpx2.Response(204),
        },
    )
    assert (
        admin.main(
            [
                "secret-repos",
                "add",
                "--org",
                "tbhb-dev",
                "--secret",
                "build",
                "widget",
                "tbhb-dev/other",
            ],
            "admin",
            runtime=runtime,
        )
        == 0
    )
    assert [(r.method, r.url.path) for r in fake.requests[2::3]] == [
        ("GET", "/orgs/tbhb-dev/actions/secrets/build"),
        ("GET", "/repos/tbhb-dev/widget"),
        ("PUT", "/orgs/tbhb-dev/actions/secrets/build/repositories/1"),
        ("GET", "/repos/tbhb-dev/other"),
        ("PUT", "/orgs/tbhb-dev/actions/secrets/build/repositories/9"),
    ]
    entries = [
        json.loads(line)
        for line in (runtime.audit.directory / "calls.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 10
    assert all(
        entry["reason"] == "add repositories to selected secret" for entry in entries
    )


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"visibility": "all"}, 200),
        ({"visibility": "private"}, 200),
        ({}, 200),
        ([], 200),
        ({}, 404),
        ("invalid", 200),
    ],
)
def test_secret_visibility_refused(
    fake: Fake, payload: object, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.reply = httpx2.Response(
        status, text=payload if isinstance(payload, str) else json.dumps(payload)
    )
    assert (
        admin.main(
            ["secret-repos", "add", "--org", "tbhb-dev", "--secret", "build", "widget"],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert len(fake.requests) == 3
    code = (
        "secret_not_selected"
        if isinstance(payload, dict) and status == 200
        else "invalid_response"
    )
    assert capsys.readouterr() == ("", f"{code}\n")


@pytest.mark.parametrize(
    ("repo", "ident", "status", "code"),
    [
        ("other/widget", 8, 204, "repository_mismatch"),
        ("../widget", 8, 204, "repository_mismatch"),
        ("tbhb-dev/a/b", 8, 204, "repository_mismatch"),
        ("widget", None, 204, "invalid_response"),
        ("widget", True, 204, "invalid_response"),
        ("widget", 0, 204, "invalid_response"),
        ("widget", -1, 204, "invalid_response"),
        ("widget", 8, 403, "request_failed"),
    ],
)
def test_repository_selection_errors(  # noqa: PLR0913, PLR0917  # Table plus fixtures.
    fake: Fake,
    repo: str,
    ident: object,
    status: int,
    code: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = selected_transport(
        fake,
        {
            "/orgs/tbhb-dev/actions/secrets/build": httpx2.Response(
                200, json={"visibility": "selected"}
            ),
            "/repos/tbhb-dev/widget": httpx2.Response(200, json={"id": ident}),
            "/orgs/tbhb-dev/actions/secrets/build/repositories/1": httpx2.Response(
                status
            ),
        },
    )
    assert (
        admin.main(
            ["secret-repos", "add", "--org", "tbhb-dev", "--secret", "build", repo],
            "admin",
            runtime=runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", f"{code}\n")


def test_invalid_org(fake: Fake, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        admin.main(
            [
                "secret-repos",
                "add",
                "--org",
                "../tbhb-dev",
                "--secret",
                "build",
                "widget",
            ],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", "ambiguous_path\n")
    assert fake.requests == fake.reads == []


def test_parser_contract() -> None:
    parser = admin.parser()
    direct = parser.parse_args(
        ["call", "PUT", "/repos/tbhb-dev/widget", "--reason", "test", "--dry-run"]
    )
    assert vars(direct) == {
        "command": "call",
        "method": "PUT",
        "path": "/repos/tbhb-dev/widget",
        "json": None,
        "reason": "test",
        "dry_run": True,
        "app": request.Settings.app,
        "item": None,
        "vault": None,
        "app_id": "5733271",
    }
    selected = parser.parse_args(
        ["secret-repos", "add", "--org", "tbhb-dev", "--secret", "build", "widget"]
    )
    assert vars(selected) == {
        "command": "secret-repos",
        "action": "add",
        "org": "tbhb-dev",
        "secret": "build",
        "repos": ["widget"],
        "reason": "add repositories to selected secret",
        "app": request.Settings.app,
        "item": None,
        "vault": None,
        "app_id": "5733271",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["secret-repos", "add", "--secret", "build", "widget"],
        ["secret-repos", "add", "--org", "tbhb-dev", "widget"],
        ["secret-repos", "remove", "--org", "tbhb-dev", "--secret", "build", "widget"],
    ],
)
def test_required_secret_arguments(argv: list[str]) -> None:
    with pytest.raises(gh_app_args.UsageError):
        admin.parser().parse_args(argv)


def test_custom_settings_and_dry_run(
    fake: Fake, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[request.Settings, str, str, object, str, bool]] = []

    def called(  # noqa: PLR0913  # One-call API seam.
        runtime: request.Runtime,
        method: str,
        path: str,
        body: object,
        reason: str,
        *,
        dry_run: bool = False,
    ) -> tuple[int, str]:
        seen.append((runtime.settings, method, path, body, reason, dry_run))
        return 400, "bad request"

    monkeypatch.setattr(admin, "call", called)
    assert (
        admin.main(
            [
                "call",
                "POST",
                "/repos/tbhb-dev/widget/pulls",
                "--reason",
                "maintenance",
                "--dry-run",
                "--app",
                "other-app",
                "--item",
                "other-item",
                "--vault",
                "other-vault",
                "--app-id",
                "42",
            ],
            "admin",
            runtime=fake.runtime,
        )
        == 1
    )
    assert seen == [
        (
            request.Settings("other-app", "other-item", "other-vault", "42"),
            "POST",
            "/repos/tbhb-dev/widget/pulls",
            None,
            "maintenance",
            True,
        )
    ]
    assert capsys.readouterr() == ("bad request", "")
