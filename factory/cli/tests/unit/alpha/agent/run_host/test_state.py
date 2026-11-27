"""Check the preparation record's handling of authentication data."""

from agent_example import paths

from tbhbdev.alpha.agent.credentials import Delivery
from tbhbdev.alpha.agent.run_host.state import Prepared


def test_prepared_auth_is_omitted_from_the_representation() -> None:
    before = "example-auth-before-launch"
    prepared = Prepared(paths("agent"), Delivery({}, None, None), before)
    assert prepared.auth_before == before
    assert before not in repr(prepared)
