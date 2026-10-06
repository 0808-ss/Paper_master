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

echo "== fetching work from bundle: $BUNDLE"
git -C "$SUB" fetch "$BUNDLE" 'refs/heads/*:refs/remotes/bundle/*'

case "$MODE" in
  ff-only) git -C "$SUB" merge --ff-only bundle/work ;;
  merge)   git -C "$SUB" merge bundle/work ;;
  rebase)  git -C "$SUB" rebase bundle/work ;;
  *) echo "unknown mode: $MODE (use ff-only|merge|rebase)" >&2; exit 1 ;;
esac

echo "== submodule status (no + prefix means aligned with the outer pointer):"
git -C "$ROOT" submodule status
