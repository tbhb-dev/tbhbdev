"""Mint a GitHub App installation token with secrets read through Connect.

    tbhbdev alpha gh-app APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]
    tbhbdev alpha gh-app APP --cooldown-until EPOCH
    tbhbdev alpha gh-app APP --cooldown-status

A mint reads the App's `app_id` and `private_key` from the `github-app-APP` item
through one `secrets.Store`, checks that the key is an RSA key, signs a JWT
with `openssl`, and exchanges it for an installation token. It writes a
`hosts.yml` in gh's layout under
`$XDG_STATE_HOME/gh-app/VAULT/APP/OWNER/REPO`, or `.../OWNER/+all` with
`--all-repos`, and prints that directory. No GitHub repository can be named
`+all`, and each vault has its own tree, so no two requests share a token. A
cached token with more than ten minutes left is reused, even during a
cooldown, because reuse calls neither GitHub nor the vault. A cooldown
refuses every other mint. Each file is synced to disk, then renamed into
place, and the directory is synced after. The installation lookup falls
through from the repository to its owner on a 404 only, and any other
refusal is an error. Nothing here reads a service account or runs `op`. The
token, the key and the JWT never reach stdout, stderr, an exception message or
a repr.
"""

import os
import sys

from tbhbdev import secrets
from tbhbdev.alpha import gh_app_runtime, gh_app_signing
from tbhbdev.alpha.gh_app_api import (
    InstallationMissingError,
    Minted,
    exchange_token,
    token_body,
)
from tbhbdev.alpha.gh_app_args import (
    USAGE,
    Args,
    HelpRequestedError,
    UsageError,
    parse_args,
    split_repo,
)
from tbhbdev.alpha.gh_app_runtime import (
    RUN_ENV,
    Env,
    GhAppError,
    Reader,
)
from tbhbdev.alpha.gh_app_state import (
    EXPIRES_FILE,
    HOSTS_FILE,
    cooldown_message,
    cooldown_path,
    cooldown_until,
    make_dir,
    out_dir,
    read_text,
    reusable,
    store_token,
    write_file,
)

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_COOLDOWN = 75


def unreadable(args: Args, name: str) -> GhAppError:
    return GhAppError(f"cannot read {name} from {args.vault}/github-app-{args.app}")


def read_field(store: Reader, args: Args, name: str) -> str:
    try:
        value = store.read(f"op://{args.vault}/github-app-{args.app}/{name}")
    except secrets.SecretsError:
        value = ""
    if not value.strip():
        raise unreadable(args, name)
    return value


def read_fields(env: Env, args: Args) -> tuple[str, str]:
    """The App's id and its PEM key, both read through one store."""
    try:
        store = env.store()
    except secrets.SecretsError:
        raise unreadable(args, "app_id") from None
    app_id = read_field(store, args, "app_id").strip()
    return app_id, gh_app_signing.rebuild_pem(read_field(store, args, "private_key"))


def request_token(env: Env, args: Args, jwt: str) -> Minted:
    try:
        return exchange_token(
            env,
            args.repo,
            jwt,
            token_body(
                args.repo, all_repos=args.all_repos, permissions=args.permissions
            ),
        )
    except InstallationMissingError:
        owner, _ = split_repo(args.repo)
        message = f"app {args.app} has no installation for {args.repo} or {owner}"
        raise GhAppError(message) from None


def mint(args: Args, env: Env) -> int:
    out = out_dir(env.state, args)
    now = env.now()
    # Reuse calls neither GitHub nor the vault, so a cooldown doesn't stop it.
    if (
        not args.dry_run
        and (out / HOSTS_FILE).is_file()
        and reusable(read_text(out / EXPIRES_FILE), now, fresh=args.fresh)
    ):
        sys.stdout.write(f"{out}\n")
        return 0
    until = cooldown_until(read_text(cooldown_path(env.state, args.app)), now)
    if until is not None:
        sys.stderr.write(f"{cooldown_message(until)}\n")
        return EXIT_COOLDOWN
    app_id, key = read_fields(env, args)
    jwt = gh_app_signing.sign(env, app_id, key)
    if args.dry_run:
        sys.stdout.write(
            f"dry run: read app_id and private_key for {args.app}, "
            "signed a JWT and made no request\n"
        )
        return 0
    store_token(args, out, request_token(env, args, jwt))
    sys.stdout.write(f"{out}\n")
    return 0


def set_cooldown(args: Args, until: int, env: Env) -> int:
    path = cooldown_path(env.state, args.app)
    make_dir(path.parent)
    write_file(path, f"{until}\n")
    return 0


def cooldown_status(args: Args, env: Env) -> int:
    until = cooldown_until(read_text(cooldown_path(env.state, args.app)), env.now())
    if until is None:
        sys.stdout.write("no cooldown\n")
        return 0
    sys.stdout.write(f"{cooldown_message(until)}\n")
    return EXIT_COOLDOWN


def dispatch(args: Args, env: Env) -> int:
    if args.cooldown_until is not None:
        return set_cooldown(args, args.cooldown_until, env)
    if args.cooldown_status:
        return cooldown_status(args, env)
    return mint(args, env)


def main(argv: list[str], prog: str, *, env: Env | None = None) -> int:
    """Run the command ARGV names with ENV's seams, which default to the real ones."""
    try:
        args = parse_args(argv)
    except HelpRequestedError:
        sys.stdout.write(USAGE.format(prog=prog))
        return 0
    except UsageError as exc:
        sys.stderr.write(f"{prog}: {exc}\n{USAGE.format(prog=prog)}")
        return EXIT_USAGE
    if os.environ.get(RUN_ENV):
        sys.stderr.write(
            f"{prog}: refused inside an agent run; a run never mints an App token\n"
        )
        return EXIT_FAILURE
    try:
        return dispatch(args, env or gh_app_runtime.default_env())
    except GhAppError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_FAILURE
    except OSError as exc:
        # An exception raised while writing state holds a path and a reason,
        # never a value.
        where = "" if exc.filename is None else f"cannot write {exc.filename}: "
        sys.stderr.write(f"{where}{exc.strerror}\n")
        return EXIT_FAILURE
