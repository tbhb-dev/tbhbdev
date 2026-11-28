"""Tests for the GET-only GitHub transport, the budget and the credential."""

import subprocess
from unittest.mock import Mock

import httpx2
import pytest

from tbhbdev.alpha import gh_shim
from tbhbdev.read import github

FAKE = "fake-read-value"  # Stub credential.
PATH = "/repos/o/r/pulls/7"
GH = "/fake/bin/gh"


class Fake:
    """A `gh` that answers `auth token`, and a mock GitHub that counts requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.requests: list[httpx2.Request] = []
        self.order: list[str] = []
        self.commands: list[list[str]] = []
        self.reply = httpx2.Response(200, json={"ok": True})
        self.fail: Exception | None = None
        self.failure: Exception | None = None
        self.code, self.out = 0, f"{FAKE}\n"
        self.reserve = Mock(side_effect=self.reserved)
        self.real_gh = Mock(return_value=GH)
        self.root = Mock(return_value="root")
        self.real = github.build_client
        monkeypatch.setattr(gh_shim, "real_gh", self.real_gh)
        monkeypatch.setattr(gh_shim, "session_root", self.root)
        monkeypatch.setattr(gh_shim, "limits", Mock(return_value=(3, 4)))
        monkeypatch.setattr(gh_shim, "reserve", self.reserve)
        monkeypatch.setattr(subprocess, "run", self.run)
        monkeypatch.setattr(github, "build_client", self.build)

    def reserved(self, *_args: object) -> None:
        self.order.append("reserve")

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.failure:
            raise self.failure
        return subprocess.CompletedProcess(command, self.code, self.out, self.out)

    def build(self, _token: str = FAKE) -> httpx2.Client:
        return self.real(FAKE, transport=httpx2.MockTransport(self.handle))

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.order.append("get")
        if self.fail:
            raise self.fail
        return self.reply


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    return Fake(monkeypatch)


def test_one_get_with_headers(fake: Fake) -> None:
    assert github.get(PATH) == {"ok": True}
    (request,) = fake.requests
    assert (request.method, str(request.url)) == (
        "GET",
        f"https://api.github.com{PATH}",
    )
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["Authorization"] == f"Bearer {FAKE}"
    assert not request.content
    assert fake.commands == [[GH, "auth", "token", "--hostname", "github.com"]]


def test_refuses_other_methods_and_bodies(fake: Fake) -> None:
    with fake.build() as c:
        for send in (
            lambda: c.post(PATH, json={}),
            lambda: c.request("GET", PATH, content=b"x"),
            lambda: c.request("GET", PATH, content=iter([b"x"])),
        ):
            with pytest.raises(github.ToolError, match="GET"):
                send()
    assert fake.requests == []


@pytest.mark.parametrize(
    ("status", "text", "message"),
    [
        (401, FAKE, "401"),
        (403, FAKE, "403"),
        (500, FAKE, "500"),
        (301, FAKE, "301"),
        (200, "not json", "malformed"),
    ],
)
def test_tool_outcomes(fake: Fake, status: int, text: str, message: str) -> None:
    fake.reply = httpx2.Response(status, text=text)
    with pytest.raises(github.ToolError, match=message) as raised:
        github.get(PATH)
    assert FAKE not in str(raised.value)


def test_not_found(fake: Fake) -> None:
    fake.reply = httpx2.Response(404)
    with pytest.raises(github.NotFoundError):
        github.get(PATH)


def test_transport_error(fake: Fake) -> None:
    fake.fail = httpx2.ConnectError(f"cannot connect with {FAKE}")
    with pytest.raises(github.ToolError, match="network") as raised:
        github.get(PATH)
    assert FAKE not in str(raised.value)


def test_budget_reserved_once_before_the_request(fake: Fake) -> None:
    github.get(PATH)
    assert fake.order == ["reserve", "get"]
    ((gh, root, now, *limits),) = (c.args for c in fake.reserve.call_args_list)
    assert (gh, root, limits) == (GH, "root", [3, 4])
    assert isinstance(now, float)


def test_no_budget_outside_a_session(fake: Fake) -> None:
    fake.root.configure_mock(return_value=None)
    github.get(PATH)
    assert fake.order == ["get"]


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (gh_shim.RefusalError("over budget: 3 calls"), "over budget: 3 calls"),
        (OSError("no state"), "no state"),
    ],
)
def test_refusal_stops_the_request(fake: Fake, error: Exception, text: str) -> None:
    fake.reserve.configure_mock(side_effect=error)
    with pytest.raises(github.ToolError, match=text):
        github.get(PATH)
    assert fake.requests == []


@pytest.mark.parametrize("failure", [OSError(FAKE), subprocess.TimeoutExpired([GH], 1)])
def test_gh_cannot_run(fake: Fake, failure: Exception) -> None:
    fake.failure = failure
    check_setup(fake)


@pytest.mark.parametrize(("code", "out"), [(0, "\n"), (3, FAKE)])
def test_no_token(fake: Fake, code: int, out: str) -> None:
    fake.code, fake.out = code, out
    check_setup(fake)


def test_no_gh(fake: Fake) -> None:
    fake.real_gh.configure_mock(return_value=None)
    check_setup(fake)


def check_setup(fake: Fake) -> None:
    with pytest.raises(github.ToolError) as raised:
        github.get(PATH)
    message = str(raised.value)
    assert "no GitHub credential" in message
    assert "`tbhbdev alpha gh-app` with `--permissions`" in message
    assert FAKE not in message
    assert fake.requests == []
    fake.reserve.assert_not_called()
