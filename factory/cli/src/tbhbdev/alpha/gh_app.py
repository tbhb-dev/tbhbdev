"""Mint a GitHub App installation token with secrets read through Connect.

    tbhbdev alpha gh-app APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]
    tbhbdev alpha gh-app APP --cooldown-until EPOCH
    tbhbdev alpha gh-app APP --cooldown-status

`mint_token` returns a repository and permission restricted token signed in memory.

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

`Env` holds the seams the command uses: the
clock, the state root, the secret store, the openssl runner and the HTTP
transport.
"""

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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
    parse_epoch,
    split_repo,
)
from tbhbdev.alpha.gh_app_runtime import (
    RUN_ENV,
    Env,
    GhAppError,
    Reader,
    fill,
)
from tbhbdev.alpha.gh_app_signing import rebuild_pem

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_COOLDOWN = 75
REUSE_MARGIN = 600
ALL_REPOS_DIR = "+all"
# mutmut changes only function bodies, so these file names, which mint and
# store_token share and the filesystem may match in any case, are constants.
HOSTS_FILE = "hosts.yml"
SLUG_FILE = "app-slug"
EXPIRES_FILE = "expires-at"


def out_dir(state: Path, args: Args) -> Path:
    owner, name = split_repo(args.repo)
    # A narrowed token has its own directory: `+` can't appear in a repository name.
    narrowed = "".join(f"+{n.replace('_', '-')}-{v}" for n, v in args.permissions)
    return (
        state
        / args.vault
        / args.app
        / owner
        / ((ALL_REPOS_DIR if args.all_repos else name) + narrowed)
    )


def cooldown_path(state: Path, app: str) -> Path:
    return state / "cooldown" / app


def cooldown_until(text: str | None, now: int) -> int | None:
    """The epoch an active cooldown lasts until, or None when there isn't one."""
    until = parse_epoch(text)
    if until is None or until <= now:
        return None
    return until


def reusable(expires: str | None, now: int, *, fresh: bool) -> bool:
    if fresh:
        return False
    epoch = parse_epoch(expires)
    return epoch is not None and epoch - now > REUSE_MARGIN


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cooldown_message(until: int) -> str:
    return f"cooldown until {utc(until)}"


def render_hosts(app: str, token: str) -> str:
    user = f'"{app}[bot]"'
    return (
        "github.com:\n"
        "    users:\n"
        f"        {user}:\n"
        f"            oauth_token: {token}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {token}\n"
        f"    user: {user}\n"
    )


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode()
    except OSError, UnicodeDecodeError:
        return None


def write_file(path: Path, text: str) -> None:
    """Write TEXT to PATH, owner-only, so that no reader sees it half-written."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        fill(handle, text)
        Path(temp).replace(path)
        sync_dir(path.parent)
    except BaseException:
        # After the rename the temp file is gone, and a failed cleanup must not
        # hide the error that brought us here.
        Path(temp).unlink(missing_ok=True)
        raise


def sync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


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
    return app_id, rebuild_pem(read_field(store, args, "private_key"))


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


def store_token(args: Args, out: Path, minted: Minted) -> None:
    make_dir(out)
    write_file(out / HOSTS_FILE, render_hosts(args.app, minted.token))
    write_file(out / SLUG_FILE, f"{args.app}\n")
    # Last, so that no reader sees a new expiry beside an old token.
    expires = 0 if args.fresh else minted.expires_at
    write_file(out / EXPIRES_FILE, f"{expires}\n")


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
