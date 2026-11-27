---
description: The YAML metadata fields of a Markdown file, their values, and how they map to commit trailers.
generated:
  - role: generated
    harness: claude-code:2.1.317
    model: anthropic:claude-opus-5-5
    effort: medium
    surface: cli
    session: sha-256-64:1b3881473f218918
    at: "2026-11-03T05:31:07Z"
status: stable
tags: [docs, metadata, schema]
title: YAML metadata reference
type: Reference
---

Each Markdown file in the repository starts with a YAML metadata block, except a reserved `index.md`, the files at the repository root, and agent instruction files (`AGENTS.md`, `CLAUDE.md`) anywhere, which have none. [YAML metadata schema](../schemas/frontmatter.schema.json) is the JSON Schema (draft 2020-12) that defines it, and it rejects any field not listed below. `bazelisk test //:okf_docs_test` validates the files against it.

## Document fields

| Field | Required | Value |
| --- | --- | --- |
| `type` | yes | One of the types below. |
| `title` | yes | A non-empty string. |
| `description` | yes | A non-empty string, reused as the index entry description. |
| `tags` | yes | A non-empty list of unique lowercase kebab-case strings. |
| `status` | yes | `draft`, `stable`, or `deprecated`. |
| `generated` | no | A non-empty list of contributor entries. |
| `verified` | no | A human sign-off. Automation leaves it unset. |

The `type` values are `Readme`, `Agent Instructions`, `Overview`, `Architecture Overview`, `Reference`, `How-to`, `Design Draft`, `Decision Record`, and `Policy`. The schema doesn't accept other values, so add a new type to it in the same change as the first doc that uses it.

## Contributor entries

The `generated` list names the agents behind the latest change to the file. Each commit an agent contributes to replaces the list with the contributors to that commit, so it may name more than one agent but doesn't grow from one commit to the next. The fields mirror the attributes of the `Generated-by` and `Assisted-by` commit trailers (`<Key>: LLM attr=value;...`), so a commit and a doc name an agent the same way. As in a trailer, you omit a field you can't determine and never guess it.

| Field | Required | Value |
| --- | --- | --- |
| `role` | yes | `generated` for `Generated-by`, or `assisted` for `Assisted-by`. |
| `harness` | yes | `claude-code`, `codex`, or `antigravity`, with an optional `:<version>`, such as `claude-code:2.1.316`. |
| `model` | yes | `<provider>:<model-id>`, such as `anthropic:claude-sonnet-5-5`. The providers are `anthropic`, `openai`, and `gcp.gemini`. |
| `at` | yes | A date-time in Coordinated Universal Time with whole seconds and a `Z` suffix, such as `2026-11-02T09:30:00Z`. Quote it. |
| `effort` | no | The harness's reasoning effort in lowercase, such as `high`. |
| `surface` | no | Where the harness ran, as it reported it, such as `cli`. No whitespace, semicolons, or commas. |
| `session` | no | `sha-256-64:` and 16 lowercase hex digits, the hash of the root session ID. |
| `subagent` | no | The same hashed form, for the subagent that did the work. Omitted for the root agent. |

A hashed ID is the leftmost 64 bits of the Secure Hash Algorithm 256 hash of the raw ID, with no secret key. Never write a raw session or subagent ID.

## Verification

`verified` is a mapping with `by` and `at`. The `by` value reads `human:<name>` with a lowercase name, and `at` has the same format as a contributor's. Agents never write it.

## Example

```yaml
---
description: How to run the checks.
generated:
  - role: generated
    harness: claude-code:2.1.316
    model: anthropic:claude-opus-5-5
    effort: high
    session: sha-256-64:3f9a2c8e1b7d4f06
    at: "2026-11-02T09:30:00Z"
status: stable
tags: [checks]
title: Running the checks
type: How-to
verified:
  by: human:tony
  at: "2026-11-02T10:15:00Z"
---
```

## Root index

Among the indexes, `docs/index.md` alone has YAML metadata. [index metadata schema](../schemas/index-frontmatter.schema.json) allows `okf_version` and nothing else, and its value is the string `"0.2"`.

## Deployment values for ops

Run these commands from the root of the public repository. Set `TBHB_PRIVATE_ROOT` when the private
companion lives outside the default sibling checkout.

```sh
private_root=${TBHB_PRIVATE_ROOT:-../tbhbdev-private}
uv run --project ops/ansible ansible-galaxy collection install \
  -r ops/ansible/requirements.yml -p ops/ansible/collections
ANSIBLE_CONFIG="$PWD/ops/ansible/ansible.cfg" \
  uv run --project ops/ansible ansible-playbook \
  -i "$private_root/ops/ansible/inventory/hosts.yml" \
  ops/ansible/playbooks/ssh-keys.yml --check
```

The deployment reads host groups, secret references, service endpoints, and paths from inventory. To establish host trust, the `ssh_host_keys` role reads the trust file next to that inventory. The ops command uses the same companion setting.

### Compose mounts

Configure service mounts before starting Compose. Read `CA_CONFIG_DIR`,
`CA_ROOTS_DIR`, `DNS_CONFIG_DIR` and `DNS_ZONES_DIR` from the companion checkout.
The private staging location supplies `CA_SECRETS_DIR` and
`CONNECT_CREDENTIALS_FILE`.

### Initialize infrastructure modules

Run `ops/tofu/tofu.sh -chdir=ops/tofu/dns init`. The wrapper
uses the companion's `ops/tofu/op.env` and the module's `backend.tfbackend`.
The backend file contains the backend settings as top-level assignments.
Set `TBHB_TOFU_OP_ENV` or `TBHB_TOFU_BACKEND_CONFIG` to use an explicit path.
Plan and apply use the companion's module variable files.

### Connect deployment

Set `OP_CONNECT_TARGET` to the companion
host name and `OP_CONNECT_DIR` to its deploy directory. The secrets step also
reads `OP_CONNECT_DEPLOY_ACCOUNT`, `OP_CONNECT_DEPLOY_VAULT` and
`OP_CONNECT_DEPLOY_ITEM` from the deployment environment.
