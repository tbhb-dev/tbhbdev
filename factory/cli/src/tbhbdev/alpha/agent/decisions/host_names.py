"""Decide whether a public name would reveal a host name."""


def contains_host_name(name: str, known_hosts: set[str]) -> bool:
    """Match known host names anywhere in NAME, ignoring case."""
    folded = name.casefold()
    return any(host.casefold() in folded for host in known_hosts)
