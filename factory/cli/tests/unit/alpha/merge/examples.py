# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Shared sample pull request data."""

GEN = (
    "Generated-by: LLM harness=claude-code:2.1.0;model=anthropic:m1;"
    "session=sha-256-64:aaaaaaaaaaaaaaaa"
)


ASSIST = "Assisted-by: LLM harness=codex;model=openai:gpt-6.1-sol"


OPENED = "Opened-by: LLM harness=claude-code:2.1.0;session=sha-256-64:aaaaaaaaaaaaaaaa"


RENOVATE_BODY = """\
This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| [macos](https://redirect.github.com/actions/runner-images) | major | `15` → `26` |

---

### Release Notes

<details>
<summary>actions/runner-images (macos)</summary>

| Announcements |
|---|
| macOS 26 is now generally available |

<table><tr><td>Kotlin</td><td>2.3.21</td></tr></table>

</details>

---

### Configuration

🚦 **Automerge**: Disabled by config.

 - [ ] If you want to rebase/retry this PR, check this box
""".replace("\n", "\r\n")


RENOVATE_AUTHOR = {"is_bot": True, "login": "app/tbhb-dev-renovate"}
