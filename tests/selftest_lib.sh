# selftest_lib.sh: hermetic scratch setup shared by the selftests (sourced, not run).
#
# Every suite calls `hag_test_setup "$S"` right after creating its scratch dir. It makes the
# run independent of this host and of the network:
#   - HAG_CONFIG points at a scratch host config, so the package's own config/host.toml
#     (data root, ledger clone, tool PATH, lease provider, cli) is never read;
#   - every other config variable is unset, so nothing leaks in from the caller's shell;
#   - WORK is a scratch git repository standing in for the spec's working dir;
#   - DOCS holds synthetic plan_state docs (with `## Current state` and `## grants-needed`),
#     written by hag_doc, so no design-repo document is read;
#   - hag_board_fixture writes a synthetic board and exports HAG_BOARD_FIXTURE, so no
#     board read reaches GitHub;
#   - HAG_FAKE_SESSIONS answers session-liveness probes without a harness CLI.
# `doctor`'s host preflight still calls `openchamber status` and `gh auth status` by design:
# probing those tools is what it is for (selftest_hermetic.sh lists them as justified).
# have_systemd says whether systemd --user job units can run here; sections that need them
# print `SKIP <reason>` otherwise (and always under HAG_TEST_NO_SYSTEMD=1).

hag_test_setup() {
  local s=$1
  unset HAG_LEDGER_DIR HAG_LEDGER_REPO HAG_TOOL_PATH HAG_CLI HAG_LEASE_CMD GPU_LEASE_CMD \
        HAG_PSI_OUT HAG_BOARD_FIXTURE
  mkdir -p "$s/data"
  printf 'data_root = "%s"\nledger_dir = "%s"\n' "$s/data" "$s/no-ledger-clone" > "$s/host.toml"
  export HAG_CONFIG="$s/host.toml"
  export HAG_DATA_ROOT="$s/data"
  # session-liveness probes answer from an empty world (no harness knows the synthetic
  # session ids anyway), so no `claude agents` / `openchamber session status` call is made
  export HAG_FAKE_SESSIONS='{"claude": [], "opencode": {}}'
  WORK="$s/work"
  mkdir -p "$WORK"
  git init -q "$WORK"
  DOCS="$s/docs"
  mkdir -p "$DOCS"
}

# hag_doc NAME: write a synthetic start doc for role NAME and print its path
hag_doc() {
  local p="$DOCS/$1_plan_state.md"
  cat > "$p" <<EOF
# $1 live plan_state (selftest)

## Mission

Synthetic role \`$1\` for the HAG selftests.

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

# hag_board_fixture PATH < JSON: write a synthetic board (a JSON list of items, board order)
hag_board_fixture() {
  cat > "$1"
  export HAG_BOARD_FIXTURE="$1"
}

have_systemd() {
  [ "${HAG_TEST_NO_SYSTEMD:-0}" != 1 ] && systemctl --user show-environment >/dev/null 2>&1
}

skip_reason() {
  if [ "${HAG_TEST_NO_SYSTEMD:-0}" = 1 ]; then echo "HAG_TEST_NO_SYSTEMD=1"
  else echo "systemctl --user show-environment failed (no systemd --user session)"; fi
}
