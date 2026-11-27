"""The agentless, config-free, batch ssh prefix the roles use."""

SSH_NO_AGENT_ARGV = (
    "ssh",
    "-F",
    "/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "IdentityAgent=none",
)
