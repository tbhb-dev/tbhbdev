#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

set -eo pipefail

mode=${1:?expected all, vale, or rumdl}
shift

debt=()
load_debt() {
  local path
  while IFS= read -r path || [[ -n $path ]]; do
    [[ -z $path || $path == \#* ]] && continue
    if [[ $path != *.md || ! -f $path ]]; then
      printf 'Prose skip list entry must name an existing Markdown file: %s\n' "$path" >&2
      exit 1
    fi
    debt+=("$path")
  done <.github/prose-lint-debt.txt
  if ((${#debt[@]})); then
    git --literal-pathspecs ls-files --error-unmatch -- "${debt[@]}" >/dev/null
  fi
}

files=()
select_file() {
  local path=${1#./} skip
  path=${path#"$PWD"/}
  for skip in "${debt[@]}"; do
    [[ $path == "$skip" ]] && return 0
  done
  files+=("$1")
}

lint_vale() (
  local work
  work=$(mktemp -d)
  trap 'rm -rf -- "$work"' EXIT
  mkdir -p "$work/.vale/config"
  cp .vale.ini "$work/"
  cp -R .vale/project "$work/.vale/"
  cp -R .vale/config/vocabularies "$work/.vale/config/"

  # Vale searches global styles before the project's freshly synced styles.
  local flags=(--no-global "--config=$work/.vale.ini")
  vale "${flags[@]}" sync
  vale "${flags[@]}" -- "${files[@]}"
)

lint_all() {
  local path
  while IFS= read -r -d '' path; do
    select_file "$path"
  done
  if ((${#files[@]})); then
    lint_vale
    cspell --no-progress -- "${files[@]}"
    rumdl check -- "${files[@]}"
  fi
}

case $mode in
all)
  load_debt
  git ls-files -z -- '*.md' | lint_all
  ;;
vale)
  load_debt
  for path; do
    select_file "$path"
  done
  if ((${#files[@]})); then
    lint_vale
  fi
  ;;
rumdl)
  load_debt
  if (($#)); then
    rumdl check -- "$@"
  fi
  ;;
*)
  printf 'Unknown prose lint mode: %s\n' "$mode" >&2
  exit 2
  ;;
esac
