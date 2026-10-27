#!/usr/bin/env bash
# ==============================================================================
# run.sh - rec-serve execution wrapper
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec mise exec -- uv run rec-serve "$@"
