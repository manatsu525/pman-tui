#!/bin/sh
set -eu

INSTALL_DIR="${PMAN_INSTALL_DIR:-/usr/local/bin}"
SHARE_DIR="${PMAN_SHARE_DIR:-/usr/local/share/pman}"
PURGE=0

say() {
    printf '%s\n' "[pman] $*"
}

die() {
    printf '%s\n' "[pman] ERROR: $*" >&2
    exit 1
}

case "${1:-}" in
    "") ;;
    --purge) PURGE=1 ;;
    -h|--help)
        cat <<'EOF'
用法：sudo sh uninstall.sh [--purge]

默认只删除程序，保留各用户的任务记录和日志。
--purge 还会删除调用用户 ~/.local/state/pman 中的数据。
EOF
        exit 0
        ;;
    *) die "未知参数：$1" ;;
esac

if [ "$(id -u)" -ne 0 ] && { [ -e "$INSTALL_DIR/pman" ] || [ -e "$SHARE_DIR/install-manifest" ]; }; then
    die "没有卸载系统文件的权限，请使用 sudo"
fi

manifest="$SHARE_DIR/install-manifest"
if [ -f "$manifest" ] &&
    grep -Fqx 'reptyr_owned=1' "$manifest" &&
    grep -Fqx "reptyr_path=$INSTALL_DIR/reptyr" "$manifest"; then
    rm -f -- "$INSTALL_DIR/reptyr"
    say "已删除由 pman 安装器编译的 $INSTALL_DIR/reptyr"
fi

rm -f -- "$INSTALL_DIR/pman"
rm -f -- "$SHARE_DIR/install-manifest" "$SHARE_DIR/uninstall.sh"
rmdir "$SHARE_DIR" 2>/dev/null || true
say "已删除 $INSTALL_DIR/pman"

if [ "$PURGE" -eq 1 ]; then
    target_user="${SUDO_USER:-${USER:-}}"
    target_home="${PMAN_TARGET_HOME:-}"
    if [ -z "$target_home" ] && [ -n "$target_user" ] && command -v getent >/dev/null 2>&1; then
        target_home=$(getent passwd "$target_user" | cut -d: -f6)
    fi
    if [ -z "$target_home" ]; then
        target_home="${HOME:-}"
    fi
    state_dir="${PMAN_STATE_DIR:-$target_home/.local/state/pman}"
    case "$state_dir" in
        ""|"/"|"/root"|"/home"|"/usr"|"/var") die "拒绝清理不安全的路径：$state_dir" ;;
    esac
    if [ -d "$state_dir" ]; then
        rm -rf -- "$state_dir"
        say "已清理 $state_dir（不可恢复）"
    else
        say "没有发现需要清理的用户数据：$state_dir"
    fi
else
    say "任务记录和日志仍保留；如需清理，请使用 --purge"
fi

say "正在运行的任务不会被停止。"
