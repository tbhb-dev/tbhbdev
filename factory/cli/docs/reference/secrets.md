---
description: Secret storage, access, and item management through 1Password.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-27T18:36:57Z"
status: stable
tags: [secrets]
title: Secrets
type: Reference
---

# Secrets

Tools access 1Password Connect through the `tbhbdev.secrets` package. To read the value of a `vault/item/field` or `op://vault/item/field` reference, call `read` in `tbhbdev.secrets.store`. That module provides `Store` to get, list, create, edit, delete, and move items.

The library reads the Connect token from the current user's own store: the agent keychain on macOS, and on Linux the file `.config/tbhbdev/secrets/connect-token` under `$HOME` (`tbhbdev.host_tokens.LINUX_TOKEN_FILE`). Linux always uses `$HOME` as the base, so configuration management can write the token without consulting `XDG_CONFIG_HOME`. Use a regular file with mode `0600` in a `0700` `secrets` directory. The current user must own both. On Linux, the reader rejects symlinks and files that grant group or other access. Only configuration management writes the Linux token file. For each request, the library tries the config's `[connect] endpoints` in order. A connection error or a 5xx answer moves it to the next endpoint, while a 4xx answer stops it. On agent hosts, the operator's host tooling sets up the config.

Tests set the `TBHBDEV_TEST_TOKEN` environment variable, and the library then uses its value in place of the keychain token. Production code leaves it unset.

`tbhbdev secret read REF` prints exactly one value, for `$(tbhbdev secret read ...)`. `get` prints the item's metadata and field labels, adding values with `--reveal`. `create` and `edit` read the fields as a JSON object of label to value from stdin or `--file`, never from the command line. `delete` asks for `--yes`.
