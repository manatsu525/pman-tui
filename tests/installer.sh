#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pman-installer-test.XXXXXX")

cleanup() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

fake_binary="$TEST_ROOT/source/pman"
mkdir -p "$(dirname -- "$fake_binary")"
printf '%s\n' '#!/bin/sh' 'test "${1:-}" = "--version" && echo "pman 1.1.3"' >"$fake_binary"
chmod 0755 "$fake_binary"
sha256sum "$fake_binary" >"$fake_binary.sha256"

PMAN_INSTALL_DIR="$TEST_ROOT/bin" \
PMAN_SHARE_DIR="$TEST_ROOT/share" \
PMAN_BINARY_SOURCE="$fake_binary" \
PMAN_CHECKSUM_SOURCE="$fake_binary.sha256" \
PMAN_UNINSTALL_SOURCE="$ROOT/uninstall.sh" \
sh "$ROOT/install.sh"

"$TEST_ROOT/bin/pman" --version | grep -q '^pman 1\.1\.3$'
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
