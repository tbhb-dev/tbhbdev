"""Verify receiver entry, identity, configuration, and refusal records."""

import io
import json
import os
import sys
from unittest.mock import Mock, call, patch
lazy from pathlib import Path

import pytest
from tbhbops.deploy import entrypoint
from tbhbops.deploy.command import CommandError
from tbhbops.deploy.protocol import (
    Json,
    RefusalError,
    Request,
)
lazy from tbhbops.deploy.config import Service

from ...deploy_model import RELEASE


def test_request_identity_and_version(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)

    def load(path: Path) -> Json:
        return (
            {"compose_binary": "/compose", "compose_version": "5.7.1"}
            if path.name == "receiver.toml"
            else {}
        )

    monkeypatch.setattr(entrypoint, "load", load)
    monkeypatch.setattr(entrypoint, "verify_binary", Mock())

    def allowed(_value: Json, _name: str) -> Service:
        return deploy_service

    def version(
        _self: entrypoint.Command,
        _args: list[str],
        _data: bytes | None = None,
        **_kwargs: object,
    ) -> bytes:
        return b"5.7.1\n"

    def run_receiver(_self: entrypoint.Receiver, *, manual: bool = False) -> int:
        return 7 if manual else 0

    monkeypatch.setattr(entrypoint, "service", allowed)
    monkeypatch.setattr(entrypoint.Command, "__call__", version)
    monkeypatch.setattr(entrypoint.Receiver, "run", run_receiver)
    assert entrypoint.receive(io.StringIO()) == 0
    assert entrypoint.receive(io.StringIO(), rollback="o11y") == 2
    monkeypatch.setenv("SUDO_USER", "operator")
    assert entrypoint.receive(io.StringIO(), rollback="o11y") == 7
    assert entrypoint.receive(io.StringIO()) == 2
    assert entrypoint.receive(io.StringIO(), rollback="bad name") == 2
    monkeypatch.setenv("SUDO_USER", "deploy")

    def wrong_version(
        _self: entrypoint.Command,
        _args: list[str],
        _data: bytes | None = None,
        **_kwargs: object,
    ) -> bytes:
        return b"5.7.0\n"

    monkeypatch.setattr(entrypoint.Command, "__call__", wrong_version)
    output = io.StringIO()
    assert entrypoint.receive(output) == 2
    assert json.loads(output.getvalue())["reason"] == "Compose version mismatch"
    monkeypatch.setattr(os, "geteuid", lambda: 1)
    output = io.StringIO()
    assert entrypoint.receive(output) == 2
    assert json.loads(output.getvalue())["release"] is None


@pytest.mark.parametrize("manual", [False, True])
def test_receive_entry_exact_construction(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch, manual: bool
) -> None:
    monkeypatch.setenv("SUDO_USER", "operator" if manual else "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE + " 456")
    service_doc: Json = {"service": "o11y"}
    config_doc: Json = {"compose_binary": "/compose", "compose_version": "5.7.1"}
    output = io.StringIO()
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(entrypoint.time, "monotonic", return_value=123),
        patch.object(entrypoint, "load", side_effect=[service_doc, config_doc]) as load,
        patch.object(entrypoint, "service", return_value=deploy_service) as service,
        patch.object(entrypoint, "Command") as command,
        patch.object(entrypoint, "verify_binary") as verify,
        patch.object(entrypoint, "Engine") as engine_type,
        patch.object(entrypoint, "Receiver") as receiver_type,
    ):
        command.return_value.return_value = b" 5.7.1\n"
        receiver_type.return_value.run.return_value = 7
        assert entrypoint.receive(output, rollback="o11y" if manual else None) == 7
    assert load.call_args_list == [
        call(entrypoint.CONFIG / "services" / "o11y.toml"),
        call(entrypoint.CONFIG / "receiver.toml"),
    ]
    service.assert_called_once_with(service_doc, "o11y")
    verify.assert_called_once_with(Path("/compose"))
    command.return_value.assert_called_once_with(
        ["/compose", "version", "--short"],
        timeout_s=10,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    engine_type.assert_called_once_with(
        deploy_service, "/compose", command.return_value
    )
    receiver_type.assert_called_once_with(
        Request("o11y", "0" * 40 if manual else RELEASE, None if manual else "456"),
        deploy_service,
        engine_type.return_value,
        output,
        123,
    )
    receiver_type.return_value.run.assert_called_once_with(manual=manual)
    assert output.getvalue() == ""


@pytest.mark.parametrize("known", [False, True])
def test_entry_refusal_record_exact(
    monkeypatch: pytest.MonkeyPatch, known: bool
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE + " 456")
    output = io.StringIO()
    with (
        patch.object(entrypoint.time, "monotonic", side_effect=[100, 102.36]),
        patch.object(os, "geteuid", return_value=0 if known else 1),
        patch.object(entrypoint, "load", side_effect=RefusalError("safe refusal")),
    ):
        assert entrypoint.receive(output) == 2
    assert (
        output.getvalue()
        == json.dumps(
            {
                "stack": "o11y" if known else None,
                "release": RELEASE if known else None,
                "outcome": "refused",
                "step": "request",
                "duration_s": 2.4,
                "run_id": "456" if known else None,
                "reason": "safe refusal" if known else "receive requires root",
            }
        )
        + "\n"
    )


def test_entry_unix_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RefusalError, match=r"^receive requires Unix$"):
        entrypoint.receive(io.StringIO())


def test_missing_original_command_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    output = io.StringIO()
    with patch.object(os, "geteuid", return_value=0):
        assert entrypoint.receive(output) == 2
    assert json.loads(output.getvalue())["stack"] is None
    assert json.loads(output.getvalue())["reason"] == "invalid request"


def test_entry_rejects_unknown_receiver_configuration(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    config_doc: Json = {
        "compose_binary": "/compose",
        "compose_version": "5.7.1",
        "unknown": True,
    }
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(entrypoint, "load", side_effect=[{}, config_doc]),
        patch.object(entrypoint, "service", return_value=deploy_service),
        patch.object(entrypoint, "Command") as command,
    ):
        output = io.StringIO()
        assert entrypoint.receive(output) == 2
    command.assert_not_called()
    assert json.loads(output.getvalue())["reason"] == "unknown object key"


@pytest.mark.parametrize(
    ("binary", "version"), [("relative-compose", "5.7.1"), ("/compose", "5.7.0")]
)
def test_entry_rejects_binary_and_configured_version_before_command(
    deploy_service: Service,
    monkeypatch: pytest.MonkeyPatch,
    binary: str,
    version: str,
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    configuration: Json = {"compose_binary": binary, "compose_version": version}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint, "load", Mock(side_effect=[{}, configuration]))
    monkeypatch.setattr(entrypoint, "service", Mock(return_value=deploy_service))
    output = io.StringIO()
    with patch.object(entrypoint, "Command") as command:
        assert entrypoint.receive(output) == 2
    command.assert_not_called()
    assert json.loads(output.getvalue())["reason"] == (
        "Compose binary path"
        if binary == "relative-compose"
        else "unsupported Compose version"
    )


@pytest.mark.parametrize("failure", [KeyError, TypeError])
def test_manual_entry_run_schema_failure_reports_failed_recovery(
    deploy_service: Service, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    monkeypatch.setenv("SUDO_USER", "operator")
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(
            entrypoint,
            "load",
            side_effect=[
                {},
                {"compose_binary": "/compose", "compose_version": "5.7.1"},
            ],
        ),
        patch.object(entrypoint, "service", return_value=deploy_service),
        patch.object(entrypoint, "verify_binary"),
        patch.object(entrypoint, "Command", return_value=Mock(return_value=b"5.7.1")),
        patch.object(entrypoint.Receiver, "run", side_effect=failure),
    ):
        output = io.StringIO()
        assert entrypoint.receive(output, rollback="o11y") == 3
    record = json.loads(output.getvalue())
    assert record["outcome"] == "failed"
    assert record["step"] == "recovery"
    assert record["reason"] == "recovery failed"


@pytest.mark.parametrize(
    ("uid", "sudo_user", "rollback", "reason"),
    [
        (1, "deploy", None, "receive requires root"),
        (0, "operator", None, "receive requires deploy sudo user"),
        (0, "deploy", "o11y", "deploy cannot request rollback"),
        (0, "operator", "bad name", "rollback service"),
    ],
)
def test_identity_refusal_reasons(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    sudo_user: str,
    rollback: str | None,
    reason: str,
) -> None:
    monkeypatch.setenv("SUDO_USER", sudo_user)
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    with pytest.raises(RefusalError, match="^" + reason + "$"):
        entrypoint.identity(rollback)


@pytest.mark.parametrize(
    "failure", [OSError, ValueError, CommandError, KeyError, TypeError]
)
def test_entry_redacts_non_protocol_errors(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    monkeypatch.setenv("SUDO_USER", "deploy")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "o11y " + RELEASE)
    output = io.StringIO()
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(entrypoint, "load", side_effect=failure("private output")),
    ):
        assert entrypoint.receive(output) == 2
    assert json.loads(output.getvalue())["reason"] == "request refused"
    assert "private output" not in output.getvalue()


@pytest.mark.parametrize(
    ("wanted", "fields"),
    [
        (None, (None, None, None)),
        (Request("o11y", RELEASE, "123"), ("o11y", RELEASE, "123")),
        (Request("o11y", RELEASE, None), ("o11y", RELEASE, None)),
    ],
)
def test_refusal_identity_preserves_only_known_protocol_fields(
    wanted: Request | None, fields: tuple[str | None, str | None, str | None]
) -> None:
    assert entrypoint.identity_fields(wanted) == fields
