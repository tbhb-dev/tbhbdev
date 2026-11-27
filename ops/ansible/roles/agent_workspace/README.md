---
type: Readme
title: Agent workspaces and Connect tokens
description: Maintain agent workspaces and install existing Connect tokens.
tags: [ansible, agents, credentials]
status: draft
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: medium
    surface: exec
    session: sha-256-64:e0e96ad5d844542e
    at: "2026-11-25T16:22:48Z"
---

<!-- cspell:words pathlib pyproject runpy tbhbops -->

`agent-workspace.yml` selects `agent_hosts` from inventory and connects as ops. The role maintains the workspace with `become_user: {{ agent_workspace_user }}`, which defaults to `agent`, and installs that account's private `gitleaks` configuration.

## Deployment vars

Clones live under the run account's home at `<home>/Code/github.com/<org>/<repo>`. On macOS that's `/Users/example-user/Code/github.com/tbhb-dev/tools`, and on Linux `/home/example-user/Code/github.com/tbhb-dev/tools`. The account comes from `agent_workspace_user`, which defaults to `agent`, and its home from `agent_workspace_home`, which defaults to `/Users/<user>` on macOS and `/home/<user>` elsewhere. A host that runs agents as `ops` sets `agent_workspace_user: ops` and gets `<ops home>/Code/...` with no other change. Its own vars fix the root at `<home>/Code`, so writes stay inside the run account's home.

The role makes `<home>/Code` at 0700. Its owner is the account and its group is the account's primary group. Each clone belongs to the same owner and group, and Git sets the file modes inside it. Other users, the operator included, can't read the workspace. When the operator needs the logs, `sudo` reads them. Clones that already exist in the tony account's home stay in place, and the role leaves them alone.

List clones in `agent_workspace_repos` as entries with a `name` of `org/repo` on GitHub. Set `public: true` to clone and fetch over HTTPS with an empty credential helper and `GIT_TERMINAL_PROMPT=0`. These repositories clone without `gh` or a GitHub credential. Public fetches use the HTTPS URL even when the origin of an existing clone uses another transport. Repositories without that key keep the `gh auth git-credential` helper and need credentials in `GH_CONFIG_DIR`.

Set `workspace: false` on an entry to exclude it from cloning and `workspace.repos` while keeping it available for `public_repos`. Entries without this key remain in the workspace.

`agent_workspace_private_clones` defaults to `true`. Set it to `false` to skip every repository without `public: true`, including existing clones. The `lab` and `lab_macos` groups turn off private clones until the GitHub App supplies the account's credential. Other hosts keep cloning both kinds. The inventory marks `tbhb-dev/tools` and `tbhb-dev/rules` public.

The agent needs git and either `uv` or mise on `agent_workspace_path`. An existing account `uv` or Homebrew `uv` takes precedence. If `uv` is absent, the role runs `mise install uv@0.13.8` (set `agent_workspace_uv_version` to change the pin) and links its binary into `~/.local/bin/uv`. This step follows the installation of mise by `ops_tbhbdev` and works on Linux and macOS. A check run skips these installs.

`agent_workspace_tools` defaults to `<home>/Code/github.com/tbhb-dev/tools`, so list `tbhb-dev/tools` in `agent_workspace_repos`. After cloning, `Ansible` fetches origin/main anonymously over HTTPS with an empty credential helper and `GIT_TERMINAL_PROMPT=0`. It refuses installation if the tools checkout is dirty (including files Git doesn't track), off main or detached, or its `HEAD` differs from the fetched origin/main. These refusals leave the installed command untouched, matching self-update's rule. With a clean checkout at origin/main, this role installs `~/.local/bin/tbhbdev` from that checkout's full commit with `uv tool install --reinstall git+file://<tools checkout>@<commit>`. This uses the [tools documentation's `uv` tool install command](https://github.com/tbhb-dev/tools#running-a-tool) with a local Git source and a commit pin. Its [pyproject.toml](https://github.com/tbhb-dev/tools/blob/main/pyproject.toml) declares the `tbhbdev` entry point and Python version, which `uv` supplies. Keeping the install here makes the checkout available before installation and the command available before token validation.

Installation reads the version from the committed `pyproject.toml` and compares `tbhbdev --version` with `tbhbdev <version> (<full commit>)` before rebuilding. Matching output leaves the install unchanged. A rebuild reports a change upon installation. Check mode skips the fetch, checkout checks, and install.

The clone helper preserves feature branches, detached heads, dirty tracked files, and local commits. It updates a clean default branch through a fast forward. Mise installs the tools configured in `<home>/Code` without changing pins or trusting new configurations.

Supply HTTPS host names on the `Tailscale` network in `agent_workspace_connect_endpoints` and exact vault names in `agent_workspace_connect_vaults`. Defaults use `https://op-connect.tailnet.example.invalid` and the vaults `host-0-shared` and `host-<inventory_hostname>`. Match these settings to the existing host token before applying.

The controller lookup runs `tbhbdev secret read` through the controller's own Connect token. It doesn't need an `op` session. Secret tasks set `no_log: true` and turn off diffs.

For the account template, populate `agent_account_tbhbdev_config` with the operator configuration's TOML tables and the agent's endpoints on the `Tailscale` network. Values support strings, integers, Boolean values, lists, nested tables, and arrays of tables. An empty dictionary preserves the existing configuration. The template rejects addresses in `[connect] endpoints` and writes at 0600 under a 0700 directory. The account role supports Linux and macOS.

```yaml
agent_workspace_repos:
  - name: tbhb-dev/tools
    public: true
  - name: tbhb-dev/rules
    public: true
  - name: tbhb-dev/tbhbdev
agent_workspace_private_clones: false
agent_workspace_connect_endpoints: [https://op-connect.tailnet.example.invalid]
agent_account_tbhbdev_config:
  connect:
    endpoints: "{{ agent_workspace_connect_endpoints }}"
  workspace:
    root: Code/github.com
    repos: >-
      {{ agent_workspace_repos |
      reject('in', agent_workspace_repos | selectattr('workspace', 'defined') |
      selectattr('workspace', 'equalto', false) | list) | map(attribute='name') | list }}
```

Set `agent_account_claude_profiles` to a dictionary of profile name to 1Password reference, in the form `vault/item/field`. The template writes each as an `[agent.claude.profiles.<name>]` table with a `token` key, and `tbhbdev` reads the token from Connect at launch. The references aren't secrets, and `tbhbdev` keeps the token off disk. The default is empty, which renders the configuration without profile tables. Agent hosts set `primary` and `secondary` in `inventory/group_vars/unix_agent_hosts.yml`. Host profile maps replace the group map, so include both profiles when overriding the secondary reference. Host account credentials use `agent_account_config_credentials_overrides`, merged recursively into the shared credentials to preserve `agent.signing`.

```yaml
agent_account_claude_profiles:
  primary: host-0-shared/primary/token
  secondary: host-0-shared/secondary/token
```

Fill the other operator configuration tables before replacing an existing deployment configuration with this example.

## Compare the account configuration before applying

The account template sets `diff: false` and `no_log: true`. `--check --diff` previews task changes but can't compare live and rendered `config.toml` content. Before a first apply, `tbhb` compares them in a protected directory on an approved controller.

The live host comparison for tbhb-dev/tbhbdev#377 remains pending.

### Prepare the live copy

1. Set `umask 077` and create a temporary directory outside the repository with mode 0700. Through approved operator access, copy the live account's `.config/tbhbdev/config.toml` into that directory as `<host>.live.toml`, mode 0600. Keep its contents out of terminal logs, chat, and git.

### Render and compare

1. From the reviewed checkout, render that host's inventory with the same `Ansible` templating used by the template tests. The following offline command writes the candidate for host-a into the protected directory. Repeat with the appropriate host name. Inventory must supply full Connect names. Use those endpoints.

   ```sh
   umask 077
   export AGENT_COMPARE_DIR=/absolute/path/to/protected-directory
   uv run python - <<'PYTHON'
   import os
   import runpy
   from pathlib import Path

   tests = runpy.run_path("packages/tbhbops/tests/test_agent_config_template.py")
   config = tests["_inventory_config"]("host-a")
   templar = tests["Templar"](
       loader=tests["DataLoader"](),
       variables={
           "agent_account_tbhbdev_config": config,
           "connect_endpoints_resolved": config["connect"]["endpoints"],
           "agent_account_claude_profiles": tests["_inventory_templar"]("host-a").template(
               tests["trust_as_template"]("{{ agent_account_claude_profiles }}")),
       },
   )
   candidate = templar.template(tests["trust_as_template"](tests["TEMPLATE"]))
   target = Path(os.environ["AGENT_COMPARE_DIR"]) / "host-a.rendered.toml"
   with target.open("x", encoding="utf-8") as stream:
       stream.write(candidate)
   PYTHON
   ```

2. Run `diff -u <host>.live.toml <host>.rendered.toml > <host>.diff` inside that directory. Exit 0 means equal, 1 means differences to review privately, and any other status is a comparison error. Read the diff in a local editor without recording its content. Check endpoints, credentials references, access roots, host rows, and any settings exclusive to the live host. Resolve every unexpected difference before applying merged code.

3. Record host names and comparison status alongside the reviewed revision. Omit other details. Keep the protected live copy for undo until the operator verifies the apply. The role also creates a mode 0600 `config.toml.<pid>.<timestamp>~` backup. Restore the saved live configuration with its original owner and mode 0600 to undo an apply. Then correct inventory through review. Delete the protected copies and diff after verification.

The installation paths in host-a through host-d inventory are unverified: version pins don't establish which directories exist. `codex_cli` manages `.local/share/codex/<version>/codex-<platform>` and optionally activates `.local/bin/codex`. It doesn't install the listed `codex-standalone` layout. Confirm all three search directories per host before deployment. The macOS temporary directory identity is also unverified and may change if the operator recreates the account. The test checks that inventory matches the fixture. Confirm it through approved operator access in a follow-up.

## Connect name expansion

The `agent_account` role also expands short Connect names before rendering the account configuration.

Short Connect host names expand at play time using `MagicDNSSuffix` from `tailscale status --json` on the target, including in check mode. `Tailscale` must be running and return a nonempty suffix. Full host names are unchanged and don't require a `Tailscale` lookup. Inventory supplies the full name. `Tailscale` status, derived URLs, and configuration rendering use `no_log` so the suffix stays out of output and diffs. The full name matches the `Tailscale` Serve Transport Layer Security (TLS) certificate. Short names are `https://` plus one DNS label, with an optional `:port` kept after the suffix. Anything else, such as a path, user information, or a full name, passes through unchanged. Set `connect_endpoints_tailscale` to the `tailscale` binary's path when it isn't on the play's PATH. It defaults to `tailscale`, and `operator_macs` sets it to the `Tailscale` app's binary.

## Token storage

`connect-token.yml` connects directly as agent using the controller's session key at `ssh_controller_key_path`, with `IdentityAgent none`. Set `connect_token_ssh_key_file` to override the key file. Its controller lookup reads `connect_token_ref`, which defaults to `host-<inventory_hostname> / connect-token / token`. @tbhb creates the item and token first, as the next section describes.

macOS keeps the token in the dedicated `ops-connect.keychain` through `security -i`, which is what `tbhbdev.secrets` reads. Linux keeps it in an agent-owned file at 0600 in a 0700 directory below the agent home, with no keyring. `connect_token_storage` and `agent_workspace_connect_storage` default to `native` on macOS and `file` on Linux, and the role refuses `native` on Linux. Credentials pass through standard input and pipes. Before writing, the role compares the stored value.

Writes over SSH on macOS remain unverified.

The role writes the file atomically. Keychain failures never trigger an automatic fallback. On Linux, tools must read this file. Other consumers need an update to the tools repository before they can read the token from that file.

## Connect tokens

Each agent host in the `agent_hosts` group has one 1Password Connect token with no expiry. The item `connect-token` in the host's own vault `host-<host>` holds it in the field `token`. The token's name is `agent-host:<host>:<UTC stamp>`, at most 64 characters, because Connect rejects longer names. An ordinary host's token reads its own vault and `host-0-shared`. A host in the `operator_macs` group, which runs the controller reads, has a token that writes `host-0-shared` and reads the `host-<host>` vault of every agent host. `tbhbops` refuses to print a script that grants a `host-storage-*` vault to an agent-host token.

`tbhbops connect token-commands [--host H ...] [--dry-run]` prints a shell script that makes the tokens, and never runs it. Hosts come from the inventory, and the server name from `[connect] server` in the `tbhbdev` configuration. @tbhb runs the script once, in their own terminal, after `op signin`. Its first step lists the account's vaults once with `op vault list --format json`. If that fails, the script exits 1 with a message before any create or grant. The script needs `host-0-shared`, each `host-<host>` it grants, and the `host-storage-*` vaults when the ops-controller section is present. For each of them, it checks the list with `jq` and runs `op vault create` if the vault name is absent, and prints `created vault <name>`. The script leaves a vault that exists alone and doesn't print a line for it. A rerun doesn't create a vault. If the list isn't JSON, the script exits 1 before it creates a vault. Every `op` line names the 1Password account with `--account`. Its next lines grant the Connect server `host-0-shared` and each vault the tokens read. Per host, it exits 1 with a message if `op item get connect-token --vault host-<host>` succeeds, and otherwise creates the token with `op connect token create` and no `--expires-in`. A pipe through `jq -R` into `op item create` stores it, which keeps the token out of every argument and out of the output. `--host` narrows the hosts, and `--dry-run` prints the same script with each `op` and `ssh` line echoed instead of run.

`tbhbops connect check [--host H] [--inventory PATH]` runs on an agent host with `OP_CONNECT_TOKEN` set. `OP_CONNECT_HOST` is optional: when unset, the check derives `https://op-connect.<MagicDNS suffix>` from `tailscale status --json`. The short name `https://op-connect` fails Transport Layer Security (TLS) verification because the certificate requires the full name. The check lists the vaults the token reads through `GET /v1/vaults`. It compares their names with the vaults the script grants that host, and prints each vault as `ok`, `missing` or `extra`. For each vault the script grants that host read-write, the check also probes a write, since no `op` command shows the permission the token inherits. It creates a scratch Secure Note through `POST /v1/vaults/{vault}/items` and then deletes it, printing `<vault> write ok` or `<vault> write denied (<HTTP status>)`. The probe skips vaults that deny writes. When a delete fails, the line shows `cleanup FAILED` with the item to remove by hand. Any `missing` or `extra` vault, any `denied` probe and any failed cleanup exits 1. The token never appears in output. The host defaults to the short host name. An agent account with no checkout passes `--inventory PATH`, the directory that holds `hosts.yml`, such as `<checkout>/ansible/inventory`, and the check reads the hosts and grants from there instead of the checkout it runs in.

### The ops-controller token

Each `operator_macs` host also has a separate ops-controller token, named `ops-controller:<host>:<UTC stamp>`. It grants read access without write access to `host-storage-nas-a` and `host-storage-nas-b`, which hold the passwords of the `ops` account on those two hosts. The agent reads `host-<host>`, so no 1Password vault holds the token. That Mac's `ops` user keychain does. `tbhbops` fixes the two host names, and the inventory has no group for them.

`tbhbdev.secrets` looks in `ops-connect.keychain` for service `ops-connect` and account `agent`. Stored under those names in the `ops` home, the token is what `sudo -u ops tbhbdev secret read the 1Password reference for host-storage-nas-a/storage-password/password` finds. Separately, the agent's home holds another copy under the same names.

For each operator Mac, `token-commands` adds these steps to the script:

- It grants the server both `host-storage-*` vaults.
- Over `ssh` as the operator, with `sudo -n -H -u ops`, it prepares the keychain and exits 1 if `ops` already holds the item.
- It creates the token with `--vault host-storage-nas-a,r --vault host-storage-nas-b,r` and no `--expires-in`.
- It exits 1 if the token is empty or has a character outside `A-Za-z0-9._-`.
- The script doesn't store a token over 4002 characters. `security -i` reads at most 4095 characters per line, and the rest of the `add-generic-password` line takes 93 of them. The script prints the token's length and the limit.
- One `ssh` session runs `security unlock-keychain -p "" ops-connect.keychain && security -i`, which runs both steps in the same session. The `add-generic-password` line goes to `security -i` on the standard input of `ssh`. No argument on either host holds the token. Afterward, the script reads the item back.
- If the token is too long or the store fails, the script runs `op connect token delete` for the new token and exits 1. It prints `deleted the ops-controller token <name> from <server>`, or, if the delete fails, asks for the delete by hand.
- `op item create` never sees the token.

Running the script takes `ssh` to the Mac and password-free `sudo` to `ops`. `--host` selects operator Macs by name.

`tbhbops connect check --ops-controller [--host H]` checks an ops-controller token. Name an `operator_macs` host with `--host`, or run it on one. Limit the token to read access on `host-storage-nas-a` and `host-storage-nas-b`. Run it as `ops`, with the token in `OP_CONNECT_TOKEN`.

## Step placement

| Previous behavior | Disposition |
| --- | --- |
| Upgrade host and endpoint discovery | Removed as unneeded. Inventory selects hosts and vars supply endpoints. |
| Upgrade clones | Moved to `agent_workspace`, as agent. |
| Upgrade host token and vault check | Moved to `agent_workspace`, using the host credential. |
| Upgrade recorded token name and 14 day expiry warning | Later, if expiring tokens remain. The plan recommends tokens without expiry. Expired credentials fail the live check. |
| Upgrade private `gitleaks` lookup and validation | Moved to the controller lookup and TOML validation before writing. |
| Upgrade agent `gitleaks` install | Moved to `agent_workspace`, at 0600 under 0700. |
| Upgrade operator `gitleaks` install | Dropped: ops roles no longer write into the operator's home. |
| Bootstrap 5 server and vault creation, including grants | Later operator provisioning. Reapply reads the existing item without writing it. |
| Bootstrap 5 creation | `tbhbops connect token-commands` prints the script, and @tbhb runs it. |
| Bootstrap 5 rotation, scope changes, and token deletion | Later `tbhbops connect` work with @tbhb. |
| Bootstrap 5 keychain setup and token install | Moved to `connect_token`, with an explicit file fallback. |
| Bootstrap 5 token verification | Moved to `agent_workspace`. Run before revoking an old token. |
| Bootstrap 5 vault reach check | `tbhbops connect check`. |
| Bootstrap 6 deployment configuration | Moved to the `agent_account` template and deployment vars. |

Bootstrap steps 1 to 4 remain in `agent_account`. Step 7's `gitleaks` generator waits for later `tbhbops` work, while step 8 becomes the workspace playbook. Tools retains `tbhbdev host` until the migration finishes.

## Verification scope

Molecule uses Ubuntu Docker containers with controller lookup fixtures. The `default` scenario runs as `agent`, with `devel` as its primary group and a `Code` directory that starts too open. With private clones turned off, the scenario rejects authenticated Git commands and checks that the role skips missing and existing private repositories. The account starts without `uv`. A mise fixture provides it before the pinned `tbhbdev` install. Verification refuses local commits, a checkout behind origin/main, dirty tracked and files Git doesn't track, side branches, and detached heads without installing the tool again. It advances the remote main and changes the recorded installed version to exercise installs the tool again from a clean checkout, then checks each repeat leaves the install unchanged. A second user's home holds known files, and the scenario checks that its listing and checksums don't change.

The `ops_account` scenario starts on a fresh host with no `Code` directory and sets `agent_workspace_user: ops`. It checks a private clone with the existing credential helper and `uv`, plus the `tbhbdev` install under the `ops` home. An assertion also rejects an unexpected `agent` home. Both scenarios check ownership, modes, and Molecule unchanged state on repeat runs. Fixtures replace `uv`, mise, Connect, `Tailscale`, and the git remotes for isolated container runs. Expansion coverage uses mixed short and full endpoint names with a placeholder MagicDNS suffix in `default`. In `ops_account`, a full name works without `Tailscale`.

Check mode skips secret reads plus credential writes. A pending token installation requires a real reapply. Real Connect access, macOS behavior, and host applies remain unverified until the coordinator checks them.
