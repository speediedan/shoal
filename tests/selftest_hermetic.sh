#!/usr/bin/env bash
# selftest_hermetic.sh: run one selftest with every network-facing tool replaced by a stub.
#
#   selftest_hermetic.sh SUITE [suite args...]     e.g. selftest_hermetic.sh selftest_h4.sh
#
# gh, az, opencode, openchamber and claude are put first on PATH as stubs that print
# "network tool called: <argv>" to stderr and exit 1, and HAG_TEST_NO_SYSTEMD=1 makes the
# systemd sections skip. The suite must still pass. Each stub also appends its line to a
# call log, because the suites capture most command output (a stub's stderr inside a
# `check` is never printed), so the suite output alone undercounts. Afterwards every call is
# listed with the suite-output count beside the call-log count; any call is a hermeticity
# leak (a code path that reached for the network or a harness).
# Exit: the suite's rc, or 3 when it passed but a stub was called.
set -u
TESTD="$(cd "$(dirname "$0")" && pwd)"
SUITE="${1:?usage: selftest_hermetic.sh SUITE [args...]}"; shift
STUBS="$(mktemp -d "${TMPDIR:-/tmp}/hag-stubs-XXXXXX")"
CALLS="$STUBS/calls.log"
: > "$CALLS"
for t in gh az opencode openchamber claude; do
  cat > "$STUBS/$t" <<EOF
#!/bin/sh
echo "network tool called: \$0 \$*" >&2
echo "network tool called: \$0 \$*" >> "$CALLS"
exit 1
EOF
  chmod +x "$STUBS/$t"
done
LOG="$STUBS/suite.log"
PATH="$STUBS:$PATH" HAG_TEST_NO_SYSTEMD=1 "$TESTD/$SUITE" "$@" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
out_hits=$(grep -c "network tool called" "$LOG" || true)
hits=$(grep -c "network tool called" "$CALLS" || true)
# Justified: `doctor`'s host preflight exists to probe exactly these two tools (it reports
# them FAIL under the stubs, which the suites tolerate). Everything else is a leak.
JUSTIFIED='^network tool called: [^ ]*/(openchamber status|gh auth status)$'
leaks=$(grep -cvE "$JUSTIFIED" "$CALLS" || true)
echo "== hermetic $SUITE: rc=$rc, network tool calls: $hits (in suite output: $out_hits; unjustified: $leaks)"
sed "s#$STUBS/##" "$CALLS" | sort | uniq -c | sort -rn
rm -rf "$STUBS"
[ "$rc" = 0 ] || exit "$rc"
[ "$leaks" = 0 ] || exit 3
exit 0
