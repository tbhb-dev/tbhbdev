"""Shared configuration for the tbhbops tests."""

from hypothesis import settings

# No per-example deadline. The default 200 ms fails property tests
# spuriously when the machine is loaded, as it is when several agents build
# at once, and a deadline measures the host, not the code under test.
settings.register_profile("tbhbops", deadline=None)
settings.load_profile("tbhbops")
