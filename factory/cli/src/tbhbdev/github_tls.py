"""TLS verification for GitHub requests."""

import ssl

import certifi


def public_roots() -> ssl.SSLContext:
    """Verify GitHub using only the bundled public certificate authorities."""
    return ssl.create_default_context(cafile=certifi.where())
