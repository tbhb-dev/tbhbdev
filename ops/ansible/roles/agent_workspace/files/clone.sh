#!/usr/bin/env bash
set -euo pipefail
host=$1
repo=$2
path=$3
public=${4:-false}
url="https://$host/$repo.git"
credentials=(-c credential.helper=)
if [[ $public == true ]]; then
  export GIT_TERMINAL_PROMPT=0
else
  credentials+=(-c 'credential.helper=!gh auth git-credential')
fi
workspace_git() {
  git -C "$path" "$@"
}
if [[ ! -e $path ]]; then
  mkdir -p "${path%/*}"
  git "${credentials[@]}" clone --quiet "$url" "$path"
  echo changed
  exit
fi
default=$(workspace_git symbolic-ref --short refs/remotes/origin/HEAD)
branch=$(workspace_git symbolic-ref --quiet --short HEAD || true)
status=$(workspace_git status --porcelain --untracked-files=no)
if [[ $branch != "${default#origin/}" || -n $status ]]; then
  echo ok
  exit
fi
if [[ $public == true ]]; then
  # Use the anonymous URL even if an existing origin names another transport.
  workspace_git "${credentials[@]}" fetch --quiet "$url" '+refs/heads/*:refs/remotes/origin/*'
else
  workspace_git "${credentials[@]}" fetch --quiet origin
fi
head=$(workspace_git rev-parse HEAD)
tip=$(workspace_git rev-parse "$default")
if [[ $head == "$tip" ]]; then
  echo ok
  exit
fi
if workspace_git merge-base --is-ancestor HEAD "$default"; then
  workspace_git merge --ff-only --quiet "$default"
  echo changed
else
  code=$?
  [[ $code == 1 ]] || exit "$code"
  echo ok
fi
