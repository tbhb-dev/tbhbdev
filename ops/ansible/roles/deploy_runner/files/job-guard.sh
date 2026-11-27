#!/bin/sh
# The job-started hook of the deploy runner: it hands the job's keys to the
# guard, and leaves no key behind unless the guard admits the job.
#
# The runner accepts only a .sh, .ps1 or .js hook, and the image needs no
# Python for this file to do its work. It must run the same under `sh` and
# under `bash --noprofile --norc -e -o pipefail`, which the runner may use
# whatever the shebang says, so every command that can fail sits in an `if`
# or after `||`. Every path is a literal and no setting comes from the
# environment, which the job controls.
#
# 1. Drain: read the FIFO to its end with builtins alone, so that the keys are
#    out of the FIFO even when python3 or coreutils is missing. The slot script
#    writes base64, because a shell variable can't hold the NUL bytes of a tar
#    stream. The FIFO opens through `exec`, whose failure `if` can see: a
#    failed redirect on the loop itself falls through in both shells. With no
#    FIFO the hook fails, and the guard never runs.
# 2. Hand over: pipe the keys to the guard through printf, a builtin, so they
#    never appear in an argument list. No here-document or here-string, which
#    a shell may back with a temporary file. The guard takes this shell's
#    process ID, and it dies when this shell does.
# 3. Admission is the guard's alone: it writes the keys into /run/job-secrets
#    as its last act, and exits 0.
# 4. Any other status, such as a guard that failed, was killed or never
#    started, empties /run/job-secrets and fails the hook. Truncation goes
#    through a builtin redirect, so the contents die even without rm.

secrets=
l=
if ! exec 3</etc/actions-runner/job-secrets; then
  exit 1
fi
while IFS= read -r l <&3 || [ -n "$l" ]; do
  secrets=$secrets$l
  l=
done
exec 3<&-

if printf '%s' "$secrets" | /usr/bin/python3 -I /etc/actions-runner/job/job-guard "$$"; then
  secrets=
  exit 0
fi

secrets=
for f in /run/job-secrets/* /run/job-secrets/.[!.]* /run/job-secrets/..?*; do
  if [ -f "$f" ]; then
    { true >"$f"; } 2>/dev/null || :
  fi
  rm -f -- "$f" 2>/dev/null || :
done
exit 1
