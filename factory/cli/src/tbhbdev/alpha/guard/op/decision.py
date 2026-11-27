# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Decide whether a 1Password invocation names the configured account."""

from tbhbdev.alpha.guard.op.file_calls import Argv


def argv_account(words: Argv) -> bool:
    """Generated argv needs the literal flag; runtime checks its value."""
    return any(
        word == "--account" or word.startswith("--account=") for word in words[1:]
    )


def valid_account(words: list[str], account: str) -> bool:
    """Require at least one flag, with every value equal to the configured one."""
    values = [
        word.partition("=")[2] for word in words[1:] if word.startswith("--account=")
    ]
    for i, word in enumerate(words):
        if word == "--account":
            if i + 1 == len(words):
                return False
            values.append(words[i + 1])
    return bool(values) and all(value == account for value in values)


def allowed(words: list[str], account: str) -> bool:
    """Generated argv needs the literal flag; shell commands need its value."""
    return (
        argv_account(words)
        if isinstance(words, Argv)
        else valid_account(words, account)
    )


def message(command: str, account: str) -> str:
    """Describe the account requirement for an offending invocation."""
    if not account:
        return f"`{command}` is blocked. Set [op] account in the user config."
    return (
        f"`{command}` is blocked. Use the configured account {account}: "
        f"add --account {account}."
    )
