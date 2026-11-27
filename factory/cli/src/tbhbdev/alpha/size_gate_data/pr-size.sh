#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:ignore textconv numstat

set -euo pipefail
# Counting logic and policy from tbhb-dev/rules at 0b12df1dd60e1f35a45695974d6a074ad47d86e7.
# Keep counting and branch exemptions in sync with lint/private/pr_size.sh.tpl.
# Keep pr-size-exempt.txt in sync with the rules copy too.
base=$1 head=$2 scc=$3 git=$4 policy=$5 default_ref=${6:-} python=$7
head_branch=${8:-} policy_base=$base
helper=${BASH_SOURCE[0]%/*}/pr-size-import
for tool in "$scc" "$git"; do
  if ! type -P -- "$tool" >/dev/null; then
    echo "pr_size_count: $tool missing or not executable" >&2
    exit 1
  fi
done
top=$("$git" rev-parse --show-toplevel)
cd "$top"
branch_exempt=false
if [[ -n $head_branch ]]; then
  while IFS= read -r pattern || [[ -n $pattern ]]; do
    [[ -z $pattern || $pattern == \#* ]] && continue
    # shellcheck disable=SC2053 # The policy contains globs.
    if [[ $head_branch == $pattern ]]; then
      branch_exempt=true
      break
    fi
  done < <("$git" show "$policy_base:.github/pr-size-exempt-branches.txt" 2>/dev/null || true)
fi
exempt_counts='{"code_lines": 0, "code_limit": 400, "raw_lines": 0, "raw_limit": 1500, "imported_lines": null, "failed": null, "branch_exempt": false, "diff_range": "", "merge_tree_command": ""}'
if [[ $branch_exempt == false && ${PR_AUTHOR:-} == 'tbhb-dev-renovate[bot]' ]]; then
  echo "$exempt_counts"
  exit 0
fi
# A repository's baseline PR is exempt while main's live tip is its single root
# commit and this PR's base is that commit. The root needs no trailer: the
# provisioning tool creates repositories with auto_init, so no root commit
# carries one.
# MAIN_TIP is a live API read the workflow passes in. A missing tip or a
# shallow clone means no exemption, and a run after main moves sees a
# different tip and applies the limit.
base_commit=$("$git" rev-parse --verify "$base^{commit}")
shallow=$("$git" rev-parse --is-shallow-repository) || shallow=true
root_count=$("$git" rev-list --count "$base_commit") || root_count=0
if [[ $branch_exempt == false && $shallow == false && $root_count == 1 &&
  ${MAIN_TIP:-} == "$base_commit" ]]; then
  echo "$exempt_counts"
  exit 0
fi
head=$("$git" rev-parse --verify "$head^{commit}")
range="$base_commit...$head"
merge_tree_command=
if [[ -n $default_ref ]]; then
  # Use only changes from the base and default branch that HEAD already has.
  shared_base=$("$git" merge-base "$base_commit" "$head")
  shared_default=$("$git" merge-base "$default_ref" "$head")
  merge_tree_args=(merge-tree --write-tree --no-messages "$shared_base" "$shared_default")
  if shared_merge=$("$git" "${merge_tree_args[@]}"); then
    range="$shared_merge..$head"
    # The reviewer's checkout may not have this tree object yet.
    merge_tree_command="git ${merge_tree_args[*]}"
  else
    [[ $? == 1 ]] || exit 1
    # Conflict markers can match literal additions. Count from the shared default commit.
    range="$shared_default..$head"
  fi
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/pr-size.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir "$work/full" "$work/added"
paths=() raw_only=() prose=()
section=paths
while IFS= read -r line; do
  case $line in
  '' | '#'*) continue ;;
  '[raw-only]') section=languages ;;
  '[prose]') section=prose ;;
  *)
    case $section in
    paths) paths+=("$line") ;;
    prose) prose+=("$line") ;;
    languages) raw_only+=("$line") ;;
    esac
    ;;
  esac
done <"$policy"
if [[ -f .github/pr-size-exempt.txt ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] || paths+=("$line")
  done <.github/pr-size-exempt.txt
fi
scan() {
  "$scc" --no-config --no-gitignore --no-ignore --no-scc-ignore --count-ignore \
    --exclude-file '' --count-as-pattern '*/BUILD:Bazel:Bazel' \
    --count-as-pattern '*.bazel:Bazel:Bazel' --gen --format csv "$1"
}
code=0 raw=0 imported_lines=null
"$git" diff --no-renames --diff-filter=AMT --name-only -z "$range" >"$work/files"
"$git" diff --no-renames --diff-filter=AMT --name-only -z "$range" \
  -- ':(glob)**/IMPORT-MAP.toml' >"$work/maps"
if [[ -s $work/maps ]]; then
  imported_lines=0
  "$python" -I "$helper" "$git" "$head" "$work/files" <"$work/maps" >"$work/imports"
  while IFS= read -r -d '' path; do
    stat=$("$git" --literal-pathspecs diff --no-ext-diff --no-textconv --numstat "$range" -- "$path")
    IFS=$'\t' read -r additions _ <<<"$stat"
    [[ $additions == - ]] || imported_lines=$((imported_lines + additions))
  done <"$work/imports"
fi
while IFS= read -r -d '' path; do
  exempt=0
  for pattern in "${paths[@]}"; do
    # shellcheck disable=SC2053 # The policy contains globs.
    if [[ $path == $pattern || /$path == $pattern || ${path##*/} == $pattern ]]; then
      exempt=1
      break
    fi
  done
  ((exempt)) && continue
  attributes=$("$git" check-attr linguist-generated linguist-vendored -- "$path")
  if awk -F': ' '$NF == "set" || $NF == "true" {found=1} END {exit !found}' <<<"$attributes"; then continue; fi
  stat=$("$git" --literal-pathspecs diff --no-ext-diff --no-textconv --numstat "$range" -- "$path")
  [[ $stat == -* ]] && continue
  name=${path##*/}
  "$git" show "$head:$path" >"$work/full/$name"
  full=$(scan "$work/full/$name")
  [[ $full == *'(gen)'* ]] && continue
  IFS=, read -r language _ < <(sed -n '2p' <<<"$full") || true
  for pattern in "${prose[@]}"; do
    # shellcheck disable=SC2053 # Prose language globs.
    if [[ $language == $pattern ]]; then
      exempt=1
      break
    fi
  done
  ((exempt)) && continue
  "$git" --literal-pathspecs diff --no-ext-diff --no-textconv --no-renames --unified=0 "$range" -- "$path" |
    awk '/^@@/ {hunk=1; next} hunk && /^\+/ {print substr($0,2)}' >"$work/added/$name"
  count=$(wc -l <"$work/added/$name")
  raw=$((raw + count))
  added=$(scan "$work/added/$name")
  IFS=, read -r language _ amount _ < <(sed -n '2p' <<<"$added") || true
  for pattern in "${raw_only[@]}"; do
    # shellcheck disable=SC2053 # Language mappings are globs too.
    if [[ $language == $pattern ]]; then
      amount=0
      break
    fi
  done
  code=$((code + ${amount:-0}))
done <"$work/files"
failed=null
if [[ $branch_exempt == false ]]; then
  if ((code > 400)); then
    failed='"code"'
  elif ((raw > 1500)); then
    failed='"raw"'
  fi
fi
printf '{"code_lines": %d, "code_limit": 400, "raw_lines": %d, "raw_limit": 1500, "imported_lines": %s, "failed": %s, "branch_exempt": %s, "diff_range": "%s", "merge_tree_command": "%s"}\n' \
  "$code" "$raw" "$imported_lines" "$failed" "$branch_exempt" "$range" "$merge_tree_command"
