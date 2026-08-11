#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
worktree_root=$(dirname -- "$script_dir")

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [main-checkout-root]" >&2
  exit 2
fi

if [ "$#" -eq 1 ]; then
  if [ ! -d "$1" ]; then
    echo "Environment root does not exist: $1" >&2
    exit 2
  fi
  environment_root=$(CDPATH= cd -- "$1" && pwd)
else
  common_git_dir=$(git -C "$worktree_root" rev-parse --path-format=absolute --git-common-dir)
  environment_root=$(dirname -- "$common_git_dir")
fi

python_executable="$environment_root/.venv/bin/python"
if [ ! -x "$python_executable" ]; then
  echo "Python environment is not executable: $python_executable" >&2
  echo "Pass the main checkout root as the first argument." >&2
  exit 2
fi

exec "$python_executable" \
  "$worktree_root/scripts/e2e_active_run_cancellation_real.py" \
  --environment-root "$environment_root" \
  --timeout-seconds 240
