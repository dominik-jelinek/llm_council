#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
echo "Starting LLM Council at http://localhost:8001"
exec uv run python -m backend.main
