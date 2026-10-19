#!/usr/bin/env bash
# Account meters have no provider calls under tests, including the native RPC transport fixture.
set -euo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
python3 "$TESTD/test_subscription_usage.py"
