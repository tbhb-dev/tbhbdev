#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -euo pipefail

check=$1
shift
case "$check" in
module-tidy)
  bazelisk mod tidy
  git diff --exit-code -- MODULE.bazel MODULE.bazel.lock
  ;;
prose)
  skip_paths=()
  exclusions=()
  while IFS= read -r path || [[ -n $path ]]; do
    [[ -z $path || $path == \#* ]] && continue
    if [[ $path != *.md || ! -f $path ]]; then
      printf 'Prose skip list entry must name an existing Markdown file: %s\n' "$path" >&2
      exit 1
    fi
    skip_paths+=("$path")
    exclusions+=(":(exclude,literal)$path")
  done <.github/prose-lint-debt.txt
  if (("${#skip_paths[@]}")); then
    git --literal-pathspecs ls-files --error-unmatch -- "${skip_paths[@]}" >/dev/null
  fi
  git ls-files -z -- '*.md' "${exclusions[@]}" >"$1/prose-files"
  status=0
  if ! (vale sync && xargs -0 -r vale <"$1/prose-files"); then status=1; fi
  xargs -0 -r cspell --no-progress <"$1/prose-files" || status=1
  xargs -0 -r rumdl check <"$1/prose-files" || status=1
  exit "$status"
  ;;
pr-size)
  policy_base=$1 head=$2 base_ref=$3 default_ref=$4 branch=$5
  base=$policy_base
  if [[ $base_ref != "$default_ref" ]]; then
    shared_base=$(git merge-base "$base" "$head")
    shared_default=$(git merge-base "$default_ref" "$head")
    if shared_tree=$(git merge-tree --write-tree --no-messages "$shared_base" "$shared_default"); then
      commit_tree=(git -c user.name=CI -c user.email=ci commit-tree)
      base=$("${commit_tree[@]}" "$shared_tree" -m 'Shared base and default branch changes')
      head=$("${commit_tree[@]}" "$head^{tree}" -p "$base" -m 'Pull request head tree')
    else
      [[ $? == 1 ]] || exit 1
      base=$shared_default
    fi
  fi
  PR_SIZE_HEAD_BRANCH=$branch PR_SIZE_POLICY_BASE=$policy_base \
    bazelisk run --config=ci @tbhb_rules//lint:pr_size -- "$base" "$head"
  ;;
design-report)
  base=$1 output=$2 root=$PWD
  tool=lib.rules.lint.private.design_report
  "${PYTHON:-python3}" -m unittest "$tool.test_design_report"
  cd "$output"
  PYTHONPATH=$root "${PYTHON:-python3}" -m "$tool.design_report" \
    "$base/factory/cli" "$root/factory/cli" >design-report.md
  ;;
scanner-checkout)
  origin=$(git remote get-url --push origin)
  git init --quiet "$1"
  git -C "$1" fetch --quiet --depth=1 "${origin%/*}/gomodscan.git" 1d7083e6ee351b0b4d7705c9c2346ad45d43127c
  git -C "$1" checkout --quiet --detach FETCH_HEAD
  ;;
vendor)
  scratch=$1
  mkdir "$scratch"
  cp factory/agent-capacity/go.{mod,sum} "$scratch/"
  GOWORK=off bazelisk run --config=ci @rules_go//go -- \
    -C "$PWD/factory/agent-capacity" mod vendor -o "$scratch/vendor"
  ;;
modules)
  scanner=$1/gomodscan
  GOWORK=off CGO_ENABLED=0 bazelisk run --config=ci @rules_go//go -- \
    -C "$1" build -mod=readonly -o "$scanner" ./cmd/gomodscan
  cd "$2"
  status=0
  "$scanner" -format sarif -o "$3" || status=$?
  if ((status > 1)); then exit "$status"; fi
  if [[ ! -s $3 ]]; then
    printf 'Module scan produced no SARIF report.\n' >&2
    exit 1
  fi
  ;;
*)
  printf 'Unknown CI check: %s\n' "$check" >&2
  exit 2
  ;;
esac
