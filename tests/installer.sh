#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pman-installer-test.XXXXXX")

cleanup() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

PMAN_INSTALL_DIR="$TEST_ROOT/bin" \
PMAN_SHARE_DIR="$TEST_ROOT/share" \
PMAN_SOURCE="$ROOT/pman.py" \
PMAN_SKIP_DEPS=1 \
sh "$ROOT/install.sh"

"$TEST_ROOT/bin/pman" --version | grep -q '^pman 1\.0\.0$'
test -x "$TEST_ROOT/share/uninstall.sh"
test -f "$TEST_ROOT/share/install-manifest"
printf '%s\n' "pre-existing reptyr" >"$TEST_ROOT/bin/reptyr"
mkdir -p "$TEST_ROOT/home/.local/state/pman"
printf '%s\n' "keep unless purged" >"$TEST_ROOT/home/.local/state/pman/test.log"

PMAN_INSTALL_DIR="$TEST_ROOT/bin" \
PMAN_SHARE_DIR="$TEST_ROOT/share" \
PMAN_TARGET_HOME="$TEST_ROOT/home" \
sh "$ROOT/uninstall.sh" --purge

test ! -e "$TEST_ROOT/bin/pman"
test ! -e "$TEST_ROOT/share/install-manifest"
test -f "$TEST_ROOT/bin/reptyr"
test ! -e "$TEST_ROOT/home/.local/state/pman"
printf '%s\n' "installer test passed"
