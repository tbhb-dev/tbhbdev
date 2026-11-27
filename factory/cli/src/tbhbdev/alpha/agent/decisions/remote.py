"""Remote shell scripts over fixed executables and untrusted run data."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

PREFIX = """PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
PATH="$HOME/.local/bin:$PATH"
export PATH
LC_ALL=C
LANG=C
export LC_ALL LANG
if test "$(uname -s)" = Darwin; then
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
export PATH
fi
"""
ROOT = f"""state="${{XDG_STATE_HOME:-$HOME/.local/state}}"
case "$state" in /*) ;; *) state="$HOME/.local/state" ;; esac
for p in "$state" "$state/tbhbdev" "$state/{files.DIRECTORY}"; do
    test ! -L "$p" || exit 65
done
d="$state/{files.DIRECTORY}"
"""
# Copy this boundary until the host and driver branches can share it.
GIT = (
    'env -i PATH="$PATH" HOME="$HOME" LANG=C LC_ALL=C '
    "GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 "
    "GIT_CONFIG_NOSYSTEM=1 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false "
)
GIT_FLAGS = (
    "git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
    "-c core.sshCommand=ssh -c credential.helper= -c core.askPass= "
    "-c protocol.ext.allow=never -c core.pager=cat "
    "-c gc.auto=0 -c maintenance.auto=false"
)
FILE_HELPERS = r"""safe_file() {
    test ! -L "$1" || exit 65
    if test -e "$1"; then test -f "$1" || exit 65; fi
}
field() {
    safe_file "$1"
    if test -f "$1"; then
        head -c 64 "$1" | od -An -v -tx1 | tr -d ' \n'
    else printf '%s' "$2"; fi
    printf '\n'
}
"""
HELPERS = (
    FILE_HELPERS
    + r"""tmux_state() {
    error=$(tmux -S "$s/agents" -f /dev/null "$1" -t "=$2" 2>&1)
    code=$?
    present=1
    if test "$code" -eq 0; then return; fi
    test "$code" -eq 1 || exit 75
    case "$error" in
        "can't find session:"*|"no server running on "*) present=0 ;;
        "error connecting to "*" (No such file or directory)") present=0 ;;
        *) exit 75 ;;
    esac
}
"""
)


def socket_root(sockets_dir: str) -> str:
    path = shlex.quote(sockets_dir)
    if not sockets_dir.startswith("/"):
        path = '"$HOME"/' + shlex.quote(sockets_dir)
    return (
        PREFIX
        + ROOT
        + f"s={path}\n"
        + """umask 077
if test ! -e "$s"; then mkdir -p -m 700 "$s" || exit "$?"; fi
test ! -L "$s" && test ! -L "$s/agents" || exit 65
test "$(find "$s" -prune -type d -user "$(id -un)" -perm 0700)" = "$s" || exit 65
"""
        + HELPERS
    )


OWNER_MATCH = (
    'test ! -L "$owner" && test -f "$owner" && '
    'test "$(cat "$owner")" = "$given" || exit 73\n'
)


def owner_input(name: str) -> str:
    return (
        PREFIX
        + ROOT
        + f'owner="$d/{name}{files.OWNER}"\n'
        + 'IFS= read -r given || exit 73\ntest -n "$given" || exit 73\n'
    )


def owned(name: str) -> str:
    """Check the owner on stdin's first line against the run's owner file.

    The owner never appears in the script, because ssh arguments reach audit
    logs. ``transport`` writes the owner as the first line of stdin.
    """
    return owner_input(name) + OWNER_MATCH


def session(name: str, command: str, sockets_dir: str) -> str:
    return owned(name) + socket_root(sockets_dir) + f"tmux_state {command} ag-{name}\n"


def program(host: Host) -> str:
    return (
        shlex.quote(host.tbhbdev)
        if host.tbhbdev is not None
        else '"$HOME/.local/bin/tbhbdev"'
    )


def installed(host: Host) -> str:
    return PREFIX + f"{program(host)} --version"


PRESSURE_CHECK = "case \"$q\" in ''|*[!0-9.]*) q=-;; esac\n"


def limits() -> str:
    """Print one line of host numbers: load, CPUs, then memory and swap in KiB.

    The fields are the 1-minute load, the logical CPU count, MemTotal,
    MemAvailable, swap total, swap used and the memory pressure. The pressure
    is Linux's `some avg10` or macOS's pressure level, and `-` where the host
    can't say, such as a kernel without /proc/pressure/memory.
    """
    linux = (
        "read l _ < /proc/loadavg || exit 1\n"
        "c=$(nproc) || exit 1\n"
        'n=$(awk -v l="$l" -v c="$c" \'/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} '
        "/^SwapTotal:/{s=$2} /^SwapFree:/{f=$2} "
        "END{if (t!~/^[0-9]+$/ || a!~/^[0-9]+$/ "
        "|| s!~/^[0-9]+$/ || f!~/^[0-9]+$/) exit 1; "
        "print l, c, t, a, s, s-f}' "
        "/proc/meminfo) || exit 1\n"
        "q=$(awk '/^some /{sub(/^avg10=/, \"\", $2); print $2}' "
        "/proc/pressure/memory 2>/dev/null)\n"
        + PRESSURE_CHECK
        + 'printf "%s %s\\n" "$n" "$q"\n'
    )
    macos = (
        "l=$(sysctl -n vm.loadavg) || exit 1\n"
        "c=$(sysctl -n hw.logicalcpu) || exit 1\n"
        "m=$(sysctl -n hw.memsize) || exit 1\n"
        "u=$(sysctl -n vm.swapusage) || exit 1\n"
        "v=$(vm_stat) || exit 1\n"
        "q=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)\n"
        + PRESSURE_CHECK
        + 'printf "%s\\n" "$v" | awk -v l="$l" -v c="$c" -v m="$m" -v u="$u" '
        '-v q="$q" \''
        "/page size of/{z=$8} "
        "/^Pages (free|inactive|speculative|purgeable):/{p+=$NF; n++} "
        'END{split(l,x," "); split(u,w,"[ =M]+"); '
        'if (z=="" || n!=4 || x[2]=="" || w[1]!="total" || w[3]!="used" '
        "|| w[2]!~/^[0-9.]+$/ || w[4]!~/^[0-9.]+$/) exit 1; "
        'printf "%s %s %d %d %d %d %s\\n", x[2], c, m/1024, p*z/1024, '
        "w[2]*1024, w[4]*1024, q}'\n"
    )
    return (
        "# tbhbdev-limits\n"
        + PREFIX
        + f'if test "$(uname -s)" = Darwin; then\n{macos}else\n{linux}fi\n'
    )


def preflight(repository: str, ref: str, expected_version: str, host: Host) -> str:
    git_host, owner, project = repo.parts(repository)
    url = f"git@{git_host}:{owner}/{project}.git"
    return (
        PREFIX + f'installed=$({program(host)} --version) || exit "$?"\n'
        "printf '%s\\n' \"$installed\"\n"
        f'test "$installed" = {shlex.quote(expected_version)} || exit 0\n'
        'cd "$HOME" || exit "$?"\n'
        + GIT
        + 'SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}" '
        + GIT_FLAGS
        + f" ls-remote --exit-code -- {shlex.quote(url)} {shlex.quote(ref)} "
        f"{shlex.quote(ref + '^{}')}"
    )


def transfer(name: str, sockets_dir: str) -> str:
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 73
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
# The owner, stdin's first line, is the atomic name claim before the archive.
IFS= read -r given || exit 73
test -n "$given" || exit 73
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( printf '%s\\n' "$given" > "$d/{name}{files.OWNER}" ) || exit 73
tar -xf - -C "$d"
"""
    )


def start(name: str, sockets_dir: str, host: Host) -> tuple[str, ...]:
    command = shlex.join(
        ("alpha", "agent", "run-host", name, "--host", host.name, "--owned")
    )
    return (
        "/bin/sh",
        "-c",
        owned(name)
        + socket_root(sockets_dir)
        + f'tmux -S "$s/agents" -f /dev/null new-session -d -s ag-{name} '
        + f"{program(host)} {command}",
    )


def read(
    name: str, sockets_dir: str, *, answer: bool = False, reason: bool = False
) -> str:
    header = (
        owned(name)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
field "$d/{name}{files.PHASE}" 756e6b6e6f776e
field "$d/{name}{files.RC}" -
safe_file "$d/{name}{files.LAUNCHED}"
if test -f "$d/{name}{files.LAUNCHED}"; then printf '1\\n'; else printf '0\\n'; fi
printf '%s\\n' "$present"
"""
    )
    log = f'"$d/{name}{files.LOG}"'
    if answer:
        return header + answer_text(name)
    if reason:
        return (
            header
            + f"""safe_file {log}
if test -f {log}; then
    awk '/^(push (skipped|refused|failed):|pr not opened:)/ ||
         /^(commit check failed:|no new commit:|review not posted:)/ {{
             last = $0
         }}
         END {{ if (last != "") print substr(last, 1, 4096) }}' {log}
fi
"""
        )
    return header + f"safe_file {log}\nif test -f {log}; then head -c 65536 {log}; fi\n"


def answer_text(name: str) -> str:
    final, output = (f'"$d/{name}{suffix}"' for suffix in (files.FINAL, files.OUT))
    return f"""safe_file {final}
safe_file {output}
if test -f {final}; then tail -c 65536 {final}
elif test -f {output}; then tail -c 65536 {output}; fi
"""


def run_rc(name: str) -> str:
    path = f'"$d/{name}{files.RC}"'
    return f"""safe_file {path}
rc=
if test -f {path}; then rc=$(head -c 5 {path}) || exit "$?"; fi
case "$rc" in ''|[0-9]|[0-9][0-9]|[0-9][0-9][0-9]) ;; *) exit 65 ;; esac
"""


def failed_log(name: str) -> str:
    log = f'"$d/{name}{files.LOG}"'
    return (
        run_rc(name)
        + f"""case "$rc" in ''|0|00|000) ;; *)
    safe_file {log}
    if test -f {log}; then tail -c 65536 {log}; fi
    exit "$?"
esac
"""
    )


def run_log(name: str) -> str:
    return owned(name) + FILE_HELPERS + failed_log(name) + answer_text(name)


def fallback(name: str, sockets_dir: str) -> str:
    rc, pending, marker = (
        f'"$d/{name}{suffix}"'
        for suffix in (files.RC, files.RC_PENDING, files.FALLBACK)
    )
    return (
        owned(name)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
safe_file {rc}
safe_file {pending}
safe_file {marker}
set -C
if test ! -f {rc}; then
    if test ! -f {marker}; then ( : > {marker} ) || exit "$?"; fi
    if test -f {pending}; then
        test ! -L "$d/trash" || exit 65
        mkdir -p "$d/trash" || exit "$?"
        stale=$(mktemp -d "$d/trash/{name}-pending.XXXXXXXX") || exit "$?"
        mv {pending} "$stale/" || exit "$?"
    fi
    printf '137\\n' > {pending} && mv -n {pending} {rc}
fi
"""
    )


def keep(name: str) -> str:
    path = f'"$d/{name}{files.KEEP}"'
    return (
        owned(name)
        + HELPERS
        + f"""umask 077
safe_file {path}
set -C
if test ! -f {path}; then ( : > {path} ); fi
"""
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
    GIT
    + 'SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}" '
    + GIT_FLAGS
    + " -c fetch.recurseSubmodules=false "
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


def mirror_command(
    repository: str, name: str, command: str, *, before: str = ""
) -> str:
    git_host, organization, project = repo.parts(repository)
    git_host = git_host.lower()
    return (
        owned(name)
        + HELPERS
        + before
        + f'mirror="$state/tbhbdev/agent-mirrors/{git_host}/{organization}/'
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


def origin_branch(repository: str, name: str) -> str:
    return mirror_command(
        repository,
        name,
        f"{MIRROR_GIT} ls-remote --end-of-options origin refs/heads/agent/{name}",
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
    return (
        owned(name)
        + FILE_HELPERS
        + run_rc(name)
        + 'test -n "$rc" || exit 65\n'
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
    return mirror_command(
        repository,
        name,
        fallback
        + f"{MIRROR_GIT} -c transfer.fsckObjects=true -c core.logAllRefUpdates=false "
        "fetch --no-write-fetch-head --no-tags --no-prune --no-prune-tags "
        f"--end-of-options origin +HEAD:{base} +{branch}:{branch} "
        '|| exit "$?"\n'
        f"perl -e {shlex.quote(INSPECTION_OUTPUT)} /bin/sh -c {shlex.quote(read)}",
        before=before,
    )


# A supervisor may hold the lock outside tmux, or leave a detached group.
OWNERLESS_LIVENESS = r"""
my ($lock, $group) = @ARGV;
if (-f $lock) {
    open(my $fh, "<", $lock) or exit 65;
    flock($fh, LOCK_EX | LOCK_NB) or exit 1;
}
if (-f $group) {
    open(my $fh, "<", $group) or exit 65;
    defined(read($fh, my $data, 4097)) && length($data) <= 4096 or exit 65;
    my $record = eval { decode_json($data) };
    ref($record) eq "HASH" or exit 65;
    my $pgid = $record->{pgid} // "";
    $pgid =~ /\A[0-9]{1,10}\z/ && $pgid > 1 && $pgid <= 2147483647 or exit 65;
    exit 1 if kill(0, -$pgid) || $! != ESRCH;
}
"""


def clean(  # noqa: PLR0913 -- separate the target from the two cleanup guards
    repository: str,
    name: str,
    stamp: str,
    sockets_dir: str,
    *,
    force: bool,
    ownerless_only: bool = False,
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    move = """check_directory() {
    if test -L "$1"; then
        printf '%s\\n' "clean refused: $1 is a symlink" >&2; exit 65
    fi
    if test -e "$1" && test ! -d "$1"; then
        printf '%s\\n' "clean refused: $1 is not a directory" >&2; exit 65
    fi
}
"""
    for component in (clone, clone / ".worktrees", clone / tree):
        move += f'check_directory "$HOME"/{shlex.quote(str(component))}\n'
    move += f"""path="$HOME"/{shlex.quote(str(clone / tree))}
if test -d "$path"; then
    test ! -e "$trash/worktree" && test ! -L "$trash/worktree" || exit 65
    mv "$path" "$trash/worktree" || exit "$?"
fi
"""
    command = (
        owner_input(name)
        + 'orphan=0\nif test ! -e "$owner" && test ! -L "$owner"; then\n'
        + "    orphan=1\nelse\n"
        + ("    exit 73\n" if ownerless_only else f"    {OWNER_MATCH}")
        + "fi\n"
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
if test "$orphan" -eq 1; then
    lock="$d/{name}{files.RUN_LOCK}"
    group="$d/{name}{files.PGID}"
    safe_file "$lock"
    safe_file "$group"
    if test -f "$lock" || test -f "$group"; then
        perl -MFcntl=:flock -MJSON::PP -MErrno=ESRCH \\
            -e {shlex.quote(OWNERLESS_LIVENESS)} "$lock" "$group" || exit "$?"
    fi
    # Claim the name just as transfer does, and keep it until all moves finish.
    mkdir -p -m 700 "$d" || exit "$?"
    (set -C; printf '%s\\n' "$given" > "$owner") || exit 73
    trap 'rm -f "$owner" || exit "$?"' EXIT
    trap 'exit 1' HUP INT TERM
fi
"""
    )
    if not force:
        command += f"""fallback="$d/{name}{files.FALLBACK}"
if test "$orphan" -eq 0 && {{ test -e "$fallback" || test -L "$fallback"; }}; then
    printf '%s\\n' 'clean refused: fallback rc requires --force' >&2
    exit 65
fi
"""
    command += (
        f"""trash="$d/trash/{stamp}-{name}"
test ! -L "$d/trash" && test ! -L "$trash" || exit 65
mkdir -p "$trash" || exit "$?"
"""
        + move
    )
    for suffix in files.SUFFIXES:
        if suffix == files.OWNER:
            command += 'test "$orphan" -eq 0 || exit 0\n'
        path = f'"$d/{name}{suffix}"'
        command += (
            f"safe_file {path}\n"
            f"if test -f {path}; then\n"
            f'    target="$trash/{name}{suffix}"\n'
            '    test ! -e "$target" && test ! -L "$target" || exit 65\n'
            f'    mv {path} "$target" || exit "$?"\n'
            "fi\n"
        )
    return command
