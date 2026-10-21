#!/usr/bin/env bash
# bundle_server.sh
# Server: fetch the `work` branch from a bundle and integrate it into the local picoquic submodule.
# Usage: bash sync/bundle_server.sh /path/to/picoquic_work.bundle [ff-only|merge|rebase]
#   ff-only (default): fast-forward only; fails if the server has local commits
#   merge:             create a merge commit (use when both sides have new commits)
#   rebase:            replay local commits on top of the bundle tip
set -euo pipefail

BUNDLE="${1:?usage: bundle_server.sh <bundle> [ff-only|merge|rebase]}"
MODE="${2:-ff-only}"

ROOT="$(git rev-parse --show-toplevel)"
SUB="$ROOT/picoquic"

if [[ "$(git -C "$SUB" symbolic-ref --short HEAD)" != work ]]; then
  echo 'picoquic must be on work; preserve local changes and select work first' >&2
  exit 1
fi
if [[ -n "$(git -C "$SUB" status --porcelain --untracked-files=no)" ]]; then
  echo 'picoquic has tracked changes; preserve them before importing' >&2
  exit 1
fi
EXPECTED="$(git -C "$ROOT" rev-parse HEAD:picoquic)"
git -C "$SUB" bundle verify "$BUNDLE"

echo "== fetching work from bundle: $BUNDLE"
git -C "$SUB" fetch --no-recurse-submodules --tags "$BUNDLE" 'refs/heads/*:refs/remotes/bundle/*'

case "$MODE" in
  ff-only) git -C "$SUB" -c submodule.recurse=false merge --ff-only bundle/work ;;
  merge)   git -C "$SUB" -c submodule.recurse=false merge bundle/work ;;
  rebase)  git -C "$SUB" -c submodule.recurse=false rebase bundle/work ;;
  *) echo "unknown mode: $MODE (use ff-only|merge|rebase)" >&2; exit 1 ;;
esac

ACTUAL="$(git -C "$SUB" rev-parse HEAD)"
if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  echo "submodule does not match outer gitlink: expected $EXPECTED, actual $ACTUAL" >&2
  echo 'Import preserved local history. Resolve the version difference before building.' >&2
  exit 1
fi

echo "== submodule status (no + prefix means aligned with the outer pointer):"
git -C "$ROOT" submodule status
