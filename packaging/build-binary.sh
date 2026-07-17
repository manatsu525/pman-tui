#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR="${PMAN_DIST_DIR:-$ROOT/dist}"
WORK_DIR="${PMAN_BUILD_DIR:-$ROOT/.build}"
REPTYR_PATH="${REPTYR_PATH:-}"

die() {
    printf '%s\n' "[pman-build] ERROR: $*" >&2
    exit 1
}

if [ -z "$REPTYR_PATH" ]; then
    REPTYR_PATH=$(command -v reptyr 2>/dev/null || true)
fi
[ -n "$REPTYR_PATH" ] && [ -x "$REPTYR_PATH" ] || \
    die "找不到可执行的 reptyr；设置 REPTYR_PATH 或先安装 reptyr"

command -v python3 >/dev/null 2>&1 || die "需要 Python 3"
if ! command -v pyinstaller >/dev/null 2>&1 && ! python3 -m PyInstaller --version >/dev/null 2>&1; then
    die "需要 PyInstaller（例如：python3 -m pip install pyinstaller）"
fi

case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=armv7 ;;
    *) arch=$(uname -m) ;;
esac

rm -rf -- "$WORK_DIR"
mkdir -p "$DIST_DIR" "$WORK_DIR"

run_pyinstaller() {
    if command -v pyinstaller >/dev/null 2>&1; then
        pyinstaller "$@"
    else
        python3 -m PyInstaller "$@"
    fi
}

run_pyinstaller \
    --clean \
    --noconfirm \
    --onefile \
    --name pman \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR/work" \
    --specpath "$WORK_DIR" \
    --hidden-import=_curses \
    --hidden-import=_curses_panel \
    --add-binary "$REPTYR_PATH:helpers" \
    "$ROOT/pman.py"

binary="$DIST_DIR/pman-linux-$arch"
mv "$DIST_DIR/pman" "$binary"
chmod 0755 "$binary"
"$binary" --version

smoke_home=$(mktemp -d "${TMPDIR:-/tmp}/pman-frozen-smoke.XXXXXX")
cleanup() {
    PMAN_HOME="$smoke_home" "$binary" _shutdown >/dev/null 2>&1 || true
    rm -rf -- "$smoke_home"
}
trap cleanup EXIT HUP INT TERM
PMAN_HOME="$smoke_home" "$binary" list --json >/dev/null
PMAN_HOME="$smoke_home" "$binary" doctor | grep -q 'reptyr:'

if command -v sha256sum >/dev/null 2>&1; then
    (
        cd "$DIST_DIR"
        sha256sum "$(basename "$binary")" >"$(basename "$binary").sha256"
    )
fi
cp "$ROOT/packaging/THIRD_PARTY_LICENSES.md" "$binary.licenses.md"
printf '%s\n' "[pman-build] generated $binary"
