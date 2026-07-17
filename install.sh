#!/bin/sh
set -eu

REPO="${PMAN_REPO:-manatsu525/pman-tui}"
RELEASE_TAG="${PMAN_RELEASE_TAG:-v1.1.3}"
INSTALL_DIR="${PMAN_INSTALL_DIR:-/usr/local/bin}"
SHARE_DIR="${PMAN_SHARE_DIR:-/usr/local/share/pman}"
DOWNLOAD_BASE="https://github.com/${REPO}/releases/download/${RELEASE_TAG}"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"
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

fetch() {
    source=$1
    destination=$2
    case "$source" in
        http://*|https://*)
            if command -v curl >/dev/null 2>&1; then
                curl -fL --retry 3 --retry-delay 1 "$source" -o "$destination"
            elif command -v wget >/dev/null 2>&1; then
                wget -qO "$destination" "$source"
            else
                die "需要 curl 或 wget 来下载独立二进制"
            fi
            ;;
        *)
            [ -f "$source" ] || die "找不到本地文件：$source"
            cp "$source" "$destination"
            ;;
    esac
}

verify_sha256() {
    binary=$1
    checksum_file=$2
    expected=$(cut -d ' ' -f1 "$checksum_file")
    [ -n "$expected" ] || die "校验文件格式异常：$checksum_file"
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$binary" | cut -d ' ' -f1)
    elif command -v openssl >/dev/null 2>&1; then
        actual=$(openssl dgst -sha256 "$binary" | cut -d '=' -f2 | tr -d ' ')
    else
        die "需要 sha256sum 或 openssl 来校验下载文件"
    fi
    [ "$actual" = "$expected" ] || die "二进制 SHA256 校验失败"
}

case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    *)
        die "当前 Release 暂时只提供 amd64 二进制（检测到 $(uname -m)）；可在该架构上运行 packaging/build-binary.sh 自行构建"
        ;;
esac

case "${1:-}" in
    -h|--help)
        cat <<EOF
用法：sh install.sh

安装器只下载预编译独立二进制，不安装 Python、curses、reptyr、Git 或编译器。

环境变量：
  PMAN_RELEASE_TAG   Release 标签（默认 ${RELEASE_TAG}）
  PMAN_BINARY_SOURCE 本地二进制路径或自定义下载 URL（测试/镜像使用）
  PMAN_CHECKSUM_SOURCE 本地 SHA256 文件或自定义校验文件 URL
  PMAN_INSTALL_DIR   可执行文件目录（默认 /usr/local/bin）
  PMAN_SHARE_DIR     卸载器和清单目录（默认 /usr/local/share/pman）
EOF
        exit 0
        ;;
    "") ;;
    *) die "未知参数：$1" ;;
esac

if [ "$(id -u)" -ne 0 ] && { [ ! -d "$INSTALL_DIR" ] || [ ! -w "$INSTALL_DIR" ]; }; then
    die "没有写入 ${INSTALL_DIR} 的权限；请使用 root 运行，或设置 PMAN_INSTALL_DIR"
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pman-install.XXXXXX")
binary_source="${PMAN_BINARY_SOURCE:-${DOWNLOAD_BASE}/pman-linux-${arch}}"
checksum_source="${PMAN_CHECKSUM_SOURCE:-${binary_source}.sha256}"
downloaded_binary="$TEMP_DIR/pman"
downloaded_checksum="$TEMP_DIR/pman.sha256"
fetch "$binary_source" "$downloaded_binary"
fetch "$checksum_source" "$downloaded_checksum"
verify_sha256 "$downloaded_binary" "$downloaded_checksum"

uninstall_source="${PMAN_UNINSTALL_SOURCE:-${RAW_BASE}/uninstall.sh}"
downloaded_uninstaller="$TEMP_DIR/uninstall.sh"
fetch "$uninstall_source" "$downloaded_uninstaller"

install -d -m 0755 "$INSTALL_DIR" "$SHARE_DIR"
install -m 0755 "$downloaded_binary" "$INSTALL_DIR/pman"
install -m 0755 "$downloaded_uninstaller" "$SHARE_DIR/uninstall.sh"

{
    printf '%s\n' "install_dir=$INSTALL_DIR"
    printf '%s\n' "source=binary"
    printf '%s\n' "release=$RELEASE_TAG"
    printf '%s\n' "reptyr_owned=0"
    printf '%s\n' "reptyr_path=$INSTALL_DIR/reptyr"
} >"$SHARE_DIR/install-manifest"

"$INSTALL_DIR/pman" --version
say "安装完成：$INSTALL_DIR/pman"
say "运行 pman 即可；诊断命令：pman doctor"
