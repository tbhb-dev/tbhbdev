"""Remote agent scripts awaiting their dedicated modules."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.decisions.remote_run import failed_log, run_rc
from tbhbdev.alpha.agent.decisions.remote_shell import (
    FILE_HELPERS,
    GIT,
    GIT_FLAGS,
    GIT_SSH,
    owned,
)

# The mirror lock serializes host fetches and reads of the fetched refs.
# Files and directories git reads inside a run's .git for a format-patch. The
# fallback refuses when any of them is a symlink.
GIT_INTERNALS = (
    "config",
    "HEAD",
    "objects",
    "refs",
    "packed-refs",
    "info",
    "shallow",
    "commondir",
    "logs",
    "logs/refs",
    "logs/refs/heads",
    "logs/refs/heads/agent",
)


MIRROR_LOCK = (
    'open(my $lock, ">>", shift) or die "open lock: $!"; '
    'flock($lock, LOCK_EX) or die "lock: $!"; '
    'system("/bin/sh", "-c", shift); '
    "exit($? == -1 ? 1 : ($? & 127) ? 128 + ($? & 127) : $? >> 8);"
)


MIRROR_GIT = (
    GIT_SSH + " -c fetch.recurseSubmodules=false "
    '-c submodule.recurse=false --git-dir="$mirror"'
)


# Read at most one byte past the display cap, then drain without buffering.
# Closing the pipe preserves the git command's failure status.
INSPECTION_OUTPUT = (
    'open(my $pipe, "-|", @ARGV) or die "open output: $!"; '
    'my $text = ""; '
    "while(length($text) < 65537 && "
    "read($pipe, $text, 65537 - length($text), length($text))) {} "
    "print substr($text, 0, 65536); "
    'print "\\n[output truncated at 65536 bytes]\\n" if length($text) > 65536; '
    "while(read($pipe, my $discard, 8192)) {} "
    "close($pipe); "
    "exit($? == -1 ? 1 : ($? & 127) ? 128 + ($? & 127) : $? >> 8);"
)


def mirror_command(repository: str, command: str) -> str:
    git_host, organization, project = repo.parts(repository)
    git_host = git_host.lower()
    return (
        f'mirror="$state/tbhbdev/agent-mirrors/{git_host}/{organization}/'
        f"""{project}.git"
for p in "$state/tbhbdev/agent-mirrors" \
    "$state/tbhbdev/agent-mirrors/{git_host}" \
    "$state/tbhbdev/agent-mirrors/{git_host}/{organization}" "$mirror"; do
    test ! -L "$p" && test -d "$p" || exit 65
done
# ls-remote can read parent config even with an invalid explicit directory.
test -d "$mirror/objects" && test -d "$mirror/refs" && \
    test -f "$mirror/HEAD" || exit 65
safe_file "$mirror/agent-mirror.lock"
export mirror
cd "$HOME" || exit "$?"
perl -MFcntl=:flock -e {shlex.quote(MIRROR_LOCK)} \
    "$mirror/agent-mirror.lock" {shlex.quote(command)}
"""
    )


def worktree_patch(repository: str, name: str, *, export: bool) -> str:
    tree = (
        repo.clone_path(PurePosixPath("."), repository) / ".worktrees" / f"agent-{name}"
    )
    paths = " ".join(
        '"$HOME"/' + shlex.quote(str(path))
        for path in (*reversed(tree.parents[:-1]), tree, tree / ".git")
    )
    run = '"$HOME"/' + shlex.quote(str(tree / ".git"))
    internals = " ".join(f'"$run/{entry}"' for entry in GIT_INTERNALS)
    base, tip = (f'"$d/{name}{suffix}"' for suffix in (files.BASE, files.HEAD))
    reflog = f'"$run/logs/refs/heads/agent/{name}"'
    git = (
        f'{GIT} GIT_CONFIG_GLOBAL=/dev/null GIT_DIR="$scratch" {GIT_FLAGS} '
        "-c protocol.allow=never -c diff.external= -c core.multiPackIndex=false "
        "--no-optional-locks"
    )
    # Read refs as data and give Git only the object directory, never the
    # run's Git configuration. Older runs get their base from branch creation.
    script = (
        f"""run={run}
check_paths() {{
    for p in {paths}; do
        test ! -L "$p" && test -d "$p" || exit 65
    done
    for p in {internals}; do
        test ! -L "$p" || exit 65
    done
    links=$(find "$run/objects" "$run/refs" -type l -print -quit) || exit 65
    test -z "$links" || exit 65
"""
        '    pack_index=$(find "$run/objects/pack" -name "multi-pack-index*" '
        "-print -quit) || exit 65\n"
        f"""    test -z "$pack_index" || exit 65
    test ! -e "$run/objects/info/alternates" || exit 65
}}
check_paths
resolve() {{
    case "$1" in *[!A-Za-z0-9._/-]*|*..*|*//*) return 65 ;; esac
    if test -f "$run/$1" && test ! -L "$run/$1"; then
        head -c 41 "$run/$1"
    else
        safe_file "$run/packed-refs"
        awk -v r="$1" '$2 == r {{print $1; exit}}' "$run/packed-refs" 2>/dev/null
    fi
}}
valid_sha() {{
    case "$1" in ""|*[!0-9a-f]*) return 65 ;; esac
    test "${{#1}}" -eq 40
}}
safe_file {tip}
if test -f {tip}; then
    tip=$(head -c 41 {tip}) || exit 65
else
    safe_file "$run/HEAD"
    tip=$(head -c 4096 "$run/HEAD") || exit 65
    case "$tip" in
        "ref: refs/heads/"*) tip=$(resolve "${{tip#ref: }}") || exit 65 ;;
    esac
fi
valid_sha "$tip" || exit 65
safe_file {base}
if test -f {base}; then
    base=$(head -c 41 {base}) || exit 65
else
    safe_file {reflog}
    base=$(head -c 256 {reflog} |
        awk 'NR == 1 && $1 ~ /^0+$/ && length($1) == 40 {{print $2}}')
fi
valid_sha "$base" || exit 65
scratch=$(mktemp -d) || exit 65
trap 'rm -rf "$scratch"' EXIT
{GIT} GIT_CONFIG_GLOBAL=/dev/null {GIT_FLAGS} \
    init -q --bare --template= "$scratch" || exit 65
ln -s "$run/objects" "$scratch/source" || exit 65
printf '%s\\n' ../source > "$scratch/objects/info/alternates" || exit 65
patch_git() {{
    {git} "$@"
}}
# Export the final tree as one commit atop the recorded base when the range
# contains merges or the tip no longer descends from the base.
merges=$(patch_git rev-list --max-count=1 --merges "$base..$tip" --) || exit 65
if test -n "$merges" || ! patch_git merge-base --is-ancestor "$base" "$tip"; then
    tree=$(patch_git rev-parse "$tip^{{tree}}") || exit 65
    base_tree=$(patch_git rev-parse "$base^{{tree}}") || exit 65
    test "$tree" != "$base_tree" || exit 0
    tip=$(patch_git -c user.name=tbhbdev -c user.email=tbhbdev@localhost \
        commit-tree "$tree" -p "$base" -m "Run {name} ($tip)") || exit 65
fi
"""
    )
    format_patch = (
        "format-patch --stdout --no-ext-diff --no-textconv --no-color "
        '--no-signature --no-attach --no-add-header --no-notes "$base..$tip" --'
    )
    if export:
        return script + f"patch_git {format_patch}\n"
    # Hold the capped output until the same path checks pass again.
    script += (
        f"perl -e {shlex.quote(INSPECTION_OUTPUT)} {git} {format_patch} "
        '> "$scratch/patch"\n'
        "status=$?\n"
        "check_paths\n"
        'cat "$scratch/patch" || exit "$?"\n'
        'exit "$status"\n'
    )
    return f"/bin/sh -c {shlex.quote(FILE_HELPERS + script)}\n"


def export_patch(repository: str, name: str) -> str:
    phase = f'"$d/{name}{files.PHASE}"'
    return (
        owned(name)
        + FILE_HELPERS
        + run_rc(name)
        + 'test -n "$rc" || exit 65\n'
        + f'safe_file {phase}\ntest "$(head -c 7 {phase})" = ended || exit 65\n'
        + worktree_patch(repository, name, export=True)
    )


def inspect(repository: str, name: str, *, limit: int | None) -> str:
    branch = f"refs/heads/agent/{name}"
    base = "refs/tbhbdev/default"
    command = "diff" if limit is None else f"log -n {limit}"
    comparison = f"{base}...{branch}" if limit is None else f"{base}..{branch}"
    read = (
        f"{MIRROR_GIT} -c diff.external= --no-optional-locks {command} "
        f"--no-ext-diff --no-textconv --no-color {comparison} --"
    )
    before = failed_log(name) if limit is not None else ""
    fallback = ""
    if limit is None:
        patch = worktree_patch(repository, name, export=False)
        before = run_rc(name) + "export rc d\n"
        fallback = (
            'if test "${rc:-0}" -ne 0 || '
            f"! pushed=$({MIRROR_GIT} ls-remote --end-of-options origin {branch}) "
            '|| test -z "$pushed"; then\n'
            f'{patch}    exit "$?"\n'
            "fi\n"
        )
    script = (
        fallback
        + f"{MIRROR_GIT} -c transfer.fsckObjects=true -c core.logAllRefUpdates=false "
        "fetch --no-write-fetch-head --no-tags --no-prune --no-prune-tags "
        f"--end-of-options origin +HEAD:{base} +{branch}:{branch} "
        '|| exit "$?"\n'
        f"perl -e {shlex.quote(INSPECTION_OUTPUT)} /bin/sh -c {shlex.quote(read)}"
    )
    return owned(name) + FILE_HELPERS + before + mirror_command(repository, script)
