#!/usr/bin/env bash
# The first run on a new Mac, before ops exists on it. @tbhb runs it once per
# Mac, as tony, with sudo. Every later apply goes through Ansible as ops.
#
#   sudo first-run-macos.sh [--dry-run] --keys FILE
#
# It installs the Command Line Tools through softwareupdate, creates ops with
# the ids and the sudoers line that the ops_accounts role uses, and authorizes
# the public keys in FILE for ops. FILE is the ops-authorized-keys file that
# playbooks/first-run.yml renders for the host. When softwareupdate offers no
# Command Line Tools label, it installs a uv-managed Python for ops instead, which
# needs uv on PATH. Safe to run again: a step that is already done changes
# nothing.
#
# --dry-run prints each change and makes none. It still lists the Command Line
# Tools labels, which creates and removes a marker file in /tmp.
#
# The only arguments are the two flags above. Nothing from an argument or from
# FILE reaches a shell: FILE's lines must each be one plain ssh public key.
#
# OPS_FIRST_RUN_ROOT prefixes every path the script writes. The tests set it;
# leave it unset, and sudo drops it from the environment anyway.
set -euo pipefail
umask 022

# These match roles/ops_accounts/vars/main.yml, and a test checks that they do.
ops_id=24369
sudoers_line='ops ALL=(ALL:ALL) NOPASSWD: ALL'
# The version the plan names for a uv-managed Python on a host with no system one.
python_version=3.13
root=${OPS_FIRST_RUN_ROOT:-}
marker=$root/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
home=$root/Users/ops
sudoers_dir=$root/etc/sudoers.d
# One key per line, with no options: command= and from= can't ride along.
key_re='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^[:cntrl:]]*)?$'

dry_run=0
keys_file=
tmp=
marker_made=0

die() {
  echo "first-run-macos: $*" >&2
  exit 1
}

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed -e '/^set -euo/d' -e 's/^# \{0,1\}//'
}

cleanup() {
  ((! marker_made)) || rm -f "$marker"
  [[ -z $tmp ]] || rm -f "$tmp"
}
trap cleanup EXIT

# step DESCRIPTION COMMAND [ARG...]: run the command, or only say so.
step() {
  local description=$1
  shift
  if ((dry_run)); then
    echo "would $description"
  else
    echo "$description"
    "$@"
  fi
}

while (($#)); do
  case $1 in
  --dry-run) dry_run=1 ;;
  --keys)
    shift
    (($#)) || die "--keys needs a file"
    keys_file=$1
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

[[ -n $keys_file ]] || die "--keys FILE is required (see --help)"
[[ -f $keys_file && -r $keys_file ]] || die "$keys_file is not a readable file"
if ((! dry_run)); then
  uid=$(id -u)
  [[ $uid -eq 0 ]] || die "run it with sudo"
fi

# Read the keys before anything changes, so a bad file stops the run early.
keys=
while IFS= read -r line || [[ -n $line ]]; do
  [[ -n $line ]] || continue
  [[ $line =~ $key_re ]] || die "$keys_file has a line that is not a plain ssh public key"
  keys=$keys$line$'\n'
done <"$keys_file"
[[ -n $keys ]] || die "$keys_file holds no keys, and ops would have no way in"

clt_installed() {
  local dir
  dir=$(xcode-select -p 2>/dev/null) && [[ -x $dir/usr/bin/python3 ]]
}

# Apple lists the Command Line Tools only while the marker file exists.
clt_label() {
  mkdir -p "${marker%/*}"
  touch "$marker"
  marker_made=1
  { softwareupdate --list 2>&1 || true; } |
    sed -n -e 's/^[[:space:]]*\*[[:space:]]*\(Label:[[:space:]]*\)\{0,1\}\(Command Line Tools.*\)$/\2/p' |
    tail -n 1
  rm -f "$marker"
  marker_made=0
}

install_clt() {
  softwareupdate --install "$1"
  clt_installed || die "softwareupdate ran, but the Command Line Tools are not usable"
}

use_uv=0
if clt_installed; then
  echo "The Command Line Tools are installed."
else
  label=$(clt_label)
  if [[ -n $label ]]; then
    step "install the Command Line Tools: $label" install_clt "$label"
  else
    echo "softwareupdate offers no Command Line Tools label, so Ansible gets a uv-managed Python $python_version for ops instead."
    uv=$(command -v uv) || die "uv is not on PATH. Install uv, then run this again."
    use_uv=1
  fi
fi

# What owns the ids, by name, so that a clash stops the run before a change.
# Lines look like "ops 24369".
owner() {
  dscl . -list "$1" "$2" | awk -v id="$ops_id" '$2 == id { print $1; exit }'
}
field() {
  dscl . -list "$1" "$2" | awk '$1 == "ops" { print $2; exit }'
}
user_uid=$(field /Users UniqueID)
uid_owner=$(owner /Users UniqueID)
group_gid=$(field /Groups PrimaryGroupID)
gid_owner=$(owner /Groups PrimaryGroupID)
[[ ${user_uid:-$ops_id} == "$ops_id" && ${uid_owner:-ops} == ops &&
  ${group_gid:-$ops_id} == "$ops_id" && ${gid_owner:-ops} == ops ]] ||
  die "ops must have uid and gid $ops_id: found ops with uid ${user_uid:-none} and group ops with gid ${group_gid:-none}, while uid $ops_id belongs to ${uid_owner:-nobody} and gid $ops_id to ${gid_owner:-nobody}. Renumber ops first."

create_group() {
  dscl . -create /Groups/ops
  dscl . -create /Groups/ops PrimaryGroupID "$ops_id"
  dscl . -create /Groups/ops Password '*'
}
create_user() {
  dscl . -create /Users/ops
  dscl . -create /Users/ops UniqueID "$ops_id"
  dscl . -create /Users/ops PrimaryGroupID "$ops_id"
  dscl . -create /Users/ops UserShell /bin/zsh
  dscl . -create /Users/ops NFSHomeDirectory /Users/ops
  dscl . -create /Users/ops RealName ops
  # No password: only a key logs in, and sudo asks for none.
  dscl . -create /Users/ops Password '*'
}
create_home() {
  mkdir -p "$home"
  chown "$ops_id:$ops_id" "$home"
  chmod 0700 "$home"
}
# Remote Login admits only this group when it exists, which is the case on a Mac
# that restricts it, so ops joins it as ops_accounts does.
join_ssh_group() {
  dseditgroup -o edit -a ops -t user com.apple.access_ssh
}
write_keys() {
  mkdir -p "$home/.ssh"
  chown "$ops_id:$ops_id" "$home/.ssh"
  chmod 0700 "$home/.ssh"
  tmp=$(mktemp "$home/.ssh/.authorized_keys.XXXXXX")
  printf '%s' "$keys" >"$tmp"
  chown "$ops_id:$ops_id" "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$home/.ssh/authorized_keys"
  tmp=
}
# sudo skips a file whose name has a dot, so the temporary file is inert until
# visudo accepts it and mv gives it its real name.
write_sudoers() {
  mkdir -p "$sudoers_dir"
  chown 0:0 "$sudoers_dir"
  chmod 0755 "$sudoers_dir"
  tmp=$(mktemp "$sudoers_dir/.ops.XXXXXX")
  printf '%s\n' "$sudoers_line" >"$tmp"
  visudo -cf "$tmp" >/dev/null
  chown 0:0 "$tmp"
  chmod 0440 "$tmp"
  mv -f "$tmp" "$sudoers_dir/ops"
  tmp=
}
install_python() {
  sudo -H -u ops "$uv" python install "$python_version"
}

[[ -n $group_gid ]] || step "create the ops group (gid $ops_id)" create_group
[[ -n $user_uid ]] || step "create the ops user (uid $ops_id)" create_user
if dscl . -read /Groups/com.apple.access_ssh >/dev/null 2>&1 &&
  ! dseditgroup -o checkmember -m ops com.apple.access_ssh >/dev/null 2>&1; then
  step "add ops to com.apple.access_ssh" join_ssh_group
fi
[[ -d $home ]] || step "create the ops home" create_home
if ! printf '%s' "$keys" | cmp -s - "$home/.ssh/authorized_keys" 2>/dev/null; then
  step "authorize the ops keys" write_keys
fi
if ! printf '%s\n' "$sudoers_line" | cmp -s - "$sudoers_dir/ops" 2>/dev/null; then
  step "grant ops sudo in $sudoers_dir/ops" write_sudoers
fi
if ((use_uv)); then
  step "install a uv-managed Python $python_version for ops" install_python
  if ((! dry_run)); then
    interpreter=$(sudo -H -u ops "$uv" python find "$python_version")
    echo "Set ansible_python_interpreter for this host to $interpreter"
  fi
fi
if ((dry_run)); then
  echo "Dry run: nothing changed."
else
  echo "Done. Ansible reaches this Mac as ops from here on."
fi
