# selftest_lib.sh: hermetic scratch setup shared by the selftests (sourced, not run).
#
# Every suite calls `shoal_test_setup "$S"` right after creating its scratch dir. It makes the
# run independent of this host and of the network:
#   - SHOAL_CONFIG points at a scratch host config, so the package's own config/host.toml
#     (data root, ledger clone, tool PATH, lease provider, cli) is never read;
#   - every other config variable is unset, so nothing leaks in from the caller's shell;
#   - WORK is a scratch git repository standing in for the spec's working dir;
#   - DOCS holds synthetic plan_state docs (with `## Current state` and `## grants-needed`),
#     written by shoal_doc, so no design-repo document is read;
#   - shoal_board_fixture writes a synthetic board and exports SHOAL_BOARD_FIXTURE, so no
#     board read reaches GitHub;
#   - SHOAL_FAKE_SESSIONS answers session-liveness probes without a harness CLI.
# `doctor`'s host preflight still calls `openchamber status` and `gh auth status` by design:
# probing those tools is what it is for (selftest_hermetic.sh lists them as justified).
# have_systemd says whether systemd --user job units can run here; sections that need them
# print `SKIP <reason>` otherwise (and always under SHOAL_TEST_NO_SYSTEMD=1).

shoal_test_setup() {
  local s=$1
  local v
  for v in LEDGER_DIR LEDGER_REPO TOOL_PATH CLI LEASE_CMD PSI_OUT BOARD_FIXTURE SPEC CONFIG DATA_ROOT; do
    unset "SHOAL_$v"
  done
  unset GPU_LEASE_CMD
  # the harness that runs a suite must not leak into it: a Claude session id rebinds watchers, and a
  # systemd INVOCATION_ID changes how `claude` is launched
  unset CLAUDE_CODE_SESSION_ID INVOCATION_ID
  # an operator often runs the suites (or `shoal upgrade`) from inside a terminal multiplexer pane:
  # nothing may reach the live herdr socket or another ledger clone from a selftest
  for v in $(env | sed -n 's/^\(HERDR_[A-Za-z0-9_]*\|ZELLIJ[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
  for v in UI_LEDGER_ROOTS HERDR_SESSION HERDR_BIN FAKE_OPENCODE_SERVER; do unset "SHOAL_$v"; done
  mkdir -p "$s/data"
  printf 'data_root = "%s"\nledger_dir = "%s"\n' "$s/data" "$s/no-ledger-clone" > "$s/host.toml"
  export SHOAL_CONFIG="$s/host.toml"
  export SHOAL_DATA_ROOT="$s/data"
  # anything shoal spawns itself (the quorum re-arming a watcher) runs THIS checkout, never the live install
  export SHOAL_ROUTE_CACHE="$s/route-cache"   # never the live viewer's route cache
  export SHOAL_CLI="python3 $(cd "$(dirname "${BASH_SOURCE[0]}")/../shoal" && pwd)/cli.py"
  # never touch the real harness skill homes: upgrade syncs skills into these
  export SHOAL_SKILL_HOMES="$s/skillhomes/a:$s/skillhomes/b"; mkdir -p "$s/skillhomes/a" "$s/skillhomes/b"
  # session-liveness probes answer from an empty world (no harness knows the synthetic
  # session ids anyway), so no `claude agents` / `openchamber session status` call is made
  export SHOAL_FAKE_SESSIONS='{"claude": [], "opencode": {}}'
  WORK="$s/work"
  mkdir -p "$WORK"
  git init -q "$WORK"
  DOCS="$s/docs"
  mkdir -p "$DOCS"
}

# shoal_doc NAME: write a synthetic start doc for role NAME and print its path
shoal_doc() {
  local p="$DOCS/$1_plan_state.md"
  cat > "$p" <<EOF
# $1 live plan_state (selftest)

## Mission

Synthetic role \`$1\` for the shoal selftests.

## Current state

Written by the selftest; never read from a design repository.

## grants-needed

- Merge PRs in the selftest repository (scope: selftest; giver: the maintainer)
- None pending beyond the above

## Next

- nothing
EOF
  printf '%s\n' "$p"
}

# shoal_board_fixture PATH < JSON: write a synthetic board (a JSON list of items, board order)
shoal_board_fixture() {
  cat > "$1"
  export SHOAL_BOARD_FIXTURE="$1"
}

have_systemd() {
  [ "${SHOAL_TEST_NO_SYSTEMD:-0}" != 1 ] && systemctl --user show-environment >/dev/null 2>&1
}

skip_reason() {
  if [ "${SHOAL_TEST_NO_SYSTEMD:-0}" = 1 ]; then echo "SHOAL_TEST_NO_SYSTEMD=1"
  else echo "systemctl --user show-environment failed (no systemd --user session)"; fi
}
