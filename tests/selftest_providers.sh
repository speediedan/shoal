#!/usr/bin/env bash
# Wait-target providers and the validity wake, with every tool call patched out.
set -euo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
python3 "$TESTD/test_providers.py"
