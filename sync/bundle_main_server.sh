#!/usr/bin/env bash
# Import only main from a local bundle, without fetching submodule remotes.
# Usage: bash sync/bundle_main_server.sh /absolute/path/paper_master_main.bundle
set -euo pipefail

main() {
  local bundle="${1:?usage: bundle_main_server.sh <main-bundle>}"
  local root expected actual
  root="$(git rev-parse --show-toplevel)"
  bundle="$(realpath -- "$bundle")"
  if [[ "$(git -C "$root" symbolic-ref --short HEAD)" != main ]]; then
    echo 'Paper_master must be on main; preserve local changes and select main first' >&2
    return 1
  fi
  # A gitlink mismatch is expected between main and core imports. Staged
  # changes are still rejected; the core importer checks its own worktree.
  if ! git -C "$root" diff --quiet --ignore-submodules=all ||
     ! git -C "$root" diff --cached --quiet; then
    echo 'Paper_master has tracked changes; preserve them before importing' >&2
    return 1
  fi
  git -C "$root" bundle verify "$bundle"
  git -C "$root" -c fetch.recurseSubmodules=false fetch --no-recurse-submodules --tags \
    "$bundle" 'refs/heads/main:refs/remotes/bundle-main/main'
  expected="$(git -C "$root" rev-parse refs/remotes/bundle-main/main)"
  git -C "$root" -c submodule.recurse=false merge --ff-only refs/remotes/bundle-main/main
  actual="$(git -C "$root" rev-parse HEAD)"
  if [[ "$actual" != "$expected" ]]; then
    echo "Main does not match bundle: expected $expected, actual $actual" >&2
    return 1
  fi
  echo "Main aligned: $actual"
  echo "Required picoquic: $(git -C "$root" rev-parse HEAD:picoquic)"
  echo 'Next: bash sync/bundle_server.sh /absolute/path/picoquic_work.bundle'
}

# Parse the invocation and exit together before merge may update this script.
main "$@"; exit $?
