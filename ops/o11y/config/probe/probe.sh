#!/bin/sh
# S3 checks against RustFS, run inside the aws-cli image with the stack's own
# key in the environment. The caller picks the subcommand.
# The SDK's default request checksums stay on throughout, since
# those are what S3-compatible stores most often get wrong.
set -eu

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
  echo "probe: $*" >&2
  exit 1
}

# list_keys <bucket> [prefix]: writes every key, one per line, to $work/keys.
# Text output prints one line per listing page, and "None" for an empty one.
# A listing that fails stops the probe: it must never read as an empty
# bucket.
list_keys() {
  aws s3api list-objects-v2 --bucket "$1" --prefix "${2:-}" --query 'Contents[].Key' --output text \
    >"$work/listing" || fail "listing $1 failed"
  tr '\t' '\n' <"$work/listing" | { grep -vx -e None -e '' || true; } >"$work/keys"
}

case ${1:-} in
# A request signed with the RustFS default login must be refused as an
# unknown access key, which RustFS answers with 403 InvalidAccessKeyId,
# while the same request with the generated key succeeds. Matching the
# error code keeps an outage from passing as a refusal.
default-credentials)
  aws s3api list-buckets >/dev/null || fail "the generated key was refused"
  if AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin aws s3api list-buckets >"$work/out" 2>&1; then
    fail "the default login was accepted"
  fi
  grep -q 'An error occurred (InvalidAccessKeyId)' "$work/out" ||
    fail "the default login failed, but not as an unknown access key"
  ;;

# Prints the number of objects in a bucket.
count)
  list_keys "$2"
  wc -l <"$work/keys" | tr -d ' '
  ;;

delete-bucket)
  aws s3 rb "s3://$2" >/dev/null
  ;;

# Prints each bucket with its creation time, one per line.
buckets)
  aws s3api list-buckets --query 'Buckets[].[Name,CreationDate]' --output text
  ;;

*)
  fail "unknown subcommand: ${1:-}"
  ;;
esac
