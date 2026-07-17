#!/bin/sh
set -eu

REPO="${PMAN_REPO:-manatsu525/pman-tui}"
REF="${PMAN_REF:-main}"
INSTALL_DIR="${PMAN_INSTALL_DIR:-/usr/local/bin}"
SHARE_DIR="${PMAN_SHARE_DIR:-/usr/local/share/pman}"
REPTYR_VERSION="reptyr-0.10.0"
REPTYR_SHA256="c6ffbc34a511ac00d072219bda30699e51f2f4eb483cbae9e32e981d49e8b380"
REPTYR_URL="https://github.com/nelhage/reptyr/archive/refs/tags/${REPTYR_VERSION}.tar.gz"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/${REF}"
REPTYR_OWNED=0
TEMP_DIR=""

say() {
    printf '%s\n' "[pman] $*"
}

die() {
    printf '%s\n' "[pman] ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf -- "$TEMP_DIR"
    fi
}
trap cleanup EXIT HUP INT TERM

need_root() {
    [ "$(id -u)" -eq 0 ] || die "请使用 root 运行：curl -fsSL ${RAW_BASE}/install.sh | sudo sh"
}

fetch() {
    source=$1
    destination=$2
    case "$source" in
        http://*|https://*)
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "$source" -o "$destination"
            elif command -v wget >/dev/null 2>&1; then
                wget -qO "$destination" "$source"
            else
                python3 - "$source" "$destination" <<'PY'
import sys
import urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
            fi
            ;;
        *)
            [ -f "$source" ] || die "找不到本地文件：$source"
            cp "$source" "$destination"
            ;;
    esac
}

python_ready() {
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import curses, fcntl, pty, selectors, termios' >/dev/null 2>&1
}

install_runtime_dependencies() {
    need_root
    say "检测到缺少运行依赖，正在使用系统包管理器安装"
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y python3 ca-certificates curl tar
        apt-get install -y reptyr || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 ca-certificates curl tar
        dnf install -y reptyr || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 ca-certificates curl tar
        yum install -y reptyr || true
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache python3 py3-curses ca-certificates curl tar
        apk add --no-cache reptyr || true
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm python ca-certificates curl tar
        pacman -S --noconfirm reptyr || true
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 python3-curses ca-certificates curl tar
        zypper --non-interactive install reptyr || true
    else
        die "未找到受支持的包管理器（apt/dnf/yum/apk/pacman/zypper）"
    fi
}

install_build_dependencies() {
    need_root
    say "系统仓库没有可用的 reptyr，准备从固定版本源码编译"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y build-essential
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y gcc make glibc-devel
    elif command -v yum >/dev/null 2>&1; then
        yum install -y gcc make glibc-devel
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache build-base linux-headers
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --needed --noconfirm base-devel
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install gcc make glibc-devel
    else
        die "无法自动安装 reptyr 的编译环境"
    fi
}

install_reptyr_from_source() {
    install_build_dependencies
    archive="$TEMP_DIR/${REPTYR_VERSION}.tar.gz"
    fetch "$REPTYR_URL" "$archive"
    python3 - "$archive" "$REPTYR_SHA256" <<'PY'
import hashlib
import sys
path, expected = sys.argv[1:]
digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
if digest != expected:
    raise SystemExit(f"reptyr archive checksum mismatch: {digest}")
PY
    tar -xzf "$archive" -C "$TEMP_DIR"
    source_dir="$TEMP_DIR/reptyr-${REPTYR_VERSION}"
    [ -d "$source_dir" ] || die "reptyr 源码归档结构异常"
    make -C "$source_dir" DISABLE_TESTS=1
    install -d -m 0755 "$INSTALL_DIR"
    install -m 0755 "$source_dir/reptyr" "$INSTALL_DIR/reptyr"
    REPTYR_OWNED=1
}

find_local_source() {
    case "$0" in
        */*)
            script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)
            if [ -f "$script_dir/pman.py" ]; then
                printf '%s\n' "$script_dir/pman.py"
                return
            fi
            ;;
    esac
    printf '%s\n' "${RAW_BASE}/pman.py"
}

find_local_uninstaller() {
    case "$0" in
        */*)
            script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)
            if [ -f "$script_dir/uninstall.sh" ]; then
                printf '%s\n' "$script_dir/uninstall.sh"
                return
            fi
            ;;
    esac
    printf '%s\n' "${RAW_BASE}/uninstall.sh"
}

case "${1:-}" in
    -h|--help)
        cat <<EOF
用法：sudo sh install.sh

环境变量：
  PMAN_INSTALL_DIR  可执行文件目录（默认 /usr/local/bin）
  PMAN_SHARE_DIR    安装清单目录（默认 /usr/local/share/pman）
  PMAN_SOURCE       pman.py 的本地路径或 URL
  PMAN_SKIP_DEPS=1  跳过系统依赖安装（用于离线/测试安装）
EOF
        exit 0
        ;;
    "") ;;
    *) die "未知参数：$1" ;;
esac

if [ "${PMAN_SKIP_DEPS:-0}" != "1" ]; then
    if ! python_ready || ! command -v reptyr >/dev/null 2>&1; then
        install_runtime_dependencies
    fi
    python_ready || die "Python 3 的 curses/PTY 标准库不可用"
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pman-install.XXXXXX")

if [ "${PMAN_SKIP_DEPS:-0}" != "1" ] && ! command -v reptyr >/dev/null 2>&1; then
    install_reptyr_from_source
fi

command -v python3 >/dev/null 2>&1 || die "需要 Python 3"
pman_source="${PMAN_SOURCE:-$(find_local_source)}"
downloaded_pman="$TEMP_DIR/pman.py"
fetch "$pman_source" "$downloaded_pman"
python3 -m py_compile "$downloaded_pman"

if [ "$(id -u)" -ne 0 ] && { [ ! -d "$INSTALL_DIR" ] || [ ! -w "$INSTALL_DIR" ]; }; then
    die "没有写入 ${INSTALL_DIR} 的权限；请使用 sudo，或设置 PMAN_INSTALL_DIR"
fi

install -d -m 0755 "$INSTALL_DIR" "$SHARE_DIR"
install -m 0755 "$downloaded_pman" "$INSTALL_DIR/pman"
uninstall_source=$(find_local_uninstaller)
fetch "$uninstall_source" "$SHARE_DIR/uninstall.sh"
chmod 0755 "$SHARE_DIR/uninstall.sh"

{
    printf '%s\n' "install_dir=$INSTALL_DIR"
    printf '%s\n' "reptyr_owned=$REPTYR_OWNED"
    printf '%s\n' "reptyr_path=$INSTALL_DIR/reptyr"
} >"$SHARE_DIR/install-manifest"

"$INSTALL_DIR/pman" --version
say "安装完成：$INSTALL_DIR/pman"
say "运行 pman 即可；诊断命令：pman doctor"

if [ -r /proc/sys/kernel/yama/ptrace_scope ]; then
    scope=$(cat /proc/sys/kernel/yama/ptrace_scope)
    if [ "$scope" != "0" ]; then
        say "提示：ptrace_scope=$scope；跨 shell 接管受系统 ptrace 权限约束，可用 pman doctor 检查"
    fi
fi
