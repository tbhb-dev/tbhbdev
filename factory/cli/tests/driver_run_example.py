"""A build request for coordinator decision tests."""

from tbhbdev.alpha.agent.model import Harness, Profile, RunKind, RunRequest

BUILD = RunRequest(
    kind=RunKind.EXEC,
    repo="example-org/example-repo",
    commit=None,
    harness=Harness.CODEX,
    model="example-model-1",
    effort="high",
    profile=Profile.BUILD,
    claude_profile=None,
    reason=None,
    account="agent",
    timeout=None,
    class_=None,
    variant=None,
    role=None,
    unsandboxed=False,
    authorized_by=None,
    startup_deadline_seconds=120,
)
