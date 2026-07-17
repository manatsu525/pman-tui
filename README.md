# pman

`pman` 是为 headless Linux 设计的轻量 TUI 进程管理器。它只依赖 Python 3
标准库，不需要图形界面，也不需要 GDB。

它既能从头启动任务，也能接管已经在普通 shell 中启动、后来被 `Ctrl-Z` 暂停的
任务。接管后进程进入 pman 持有的伪终端（PTY），输出持续写入日志，日志目标还可
以在运行中切换。

## 最常用场景：接管一个跑太久的前台进程

先像平常一样直接运行命令：

```bash
python3 long_job.py
```

发现它要跑很久时按 `Ctrl-Z`，然后直接打开：

```bash
pman
```

TUI 会扫描整个 `/proc`，把这个任务显示为 `stopped*`，且默认放在列表顶部。即使从
另一个 SSH 会话或新 shell 打开 pman，只要进程仍然存在，就可以找到它：

- 按 `i`：接管、继续在后台运行，输出进入默认日志；
- 按 `o`：先指定日志文件，再接管并继续在后台运行；
- 按 `Enter`：接管后立即附着回前台。

不需要先执行 `bg` 或 `disown`，也不需要手工调用 GDB/reptyr。

Linux 没有普通 API 能直接替换任意进程已经打开的 `stdout`/`stderr`，所以 pman 在
内部调用 `reptyr`，通过 ptrace 把目标迁移到 pman 的 PTY：

```text
程序 stdin/stdout/stderr ↔ PTY ↔ pman daemon ┬→ 当前前台终端
                                              └→ 可随时切换的日志文件
```

接管之后，关闭 SSH、退出 TUI 或从前台脱离，任务都继续运行，后续输出不会丢失。
接管之前已经打印到旧终端的内容无法倒追回来。

## 安装

在 amd64 的 Debian/Ubuntu、Fedora/RHEL 等 glibc Linux VPS 上可以一条命令安装：

```bash
curl -fsSL https://raw.githubusercontent.com/manatsu525/pman-tui/main/install.sh | sh
```

服务器只有 `wget` 时：

```bash
wget -qO- https://raw.githubusercontent.com/manatsu525/pman-tui/main/install.sh | sh
```

安装器会从 GitHub Release 下载对应的独立二进制，校验 SHA256 后安装到
`/usr/local/bin/pman`。目标机器不需要 Python、curses、`reptyr`、Git、编译器或包管理器。
远程一行命令本身需要机器上已有 `curl` 或 `wget` 之一；如果两者都没有，先准备一个
下载工具，或把二进制和 `uninstall.sh` 复制到服务器后执行。

```bash
sh install.sh
```

安装完成后运行：

```bash
pman
```

自定义安装目录（默认值通常已经在 `PATH` 中）：

```bash
PMAN_INSTALL_DIR=/opt/pman/bin PMAN_SHARE_DIR=/opt/pman/share sh install.sh
```

### 一键卸载

删除程序但保留任务记录和日志：

```bash
curl -fsSL https://raw.githubusercontent.com/manatsu525/pman-tui/main/uninstall.sh | sh
```

同时删除当前 root 用户在 `~/.local/state/pman` 下的记录和日志：

```bash
curl -fsSL https://raw.githubusercontent.com/manatsu525/pman-tui/main/uninstall.sh | sh -s -- --purge
```

卸载不会终止正在运行的任务；二进制内置的 `reptyr` 也不会影响系统已有的 `reptyr`。

### 独立二进制部署

如果 Release 中没有你的架构，或你想自行构建，可以在一台构建机上生成单文件 Linux
二进制。二进制会内置 Python 运行时、curses 模块和 `reptyr` helper；目标机只需要
Linux 内核提供 `/proc`、PTY、ptrace，以及兼容的 glibc。

构建机需要 Python 3、PyInstaller、编译器和 `reptyr`：

```bash
python3 -m pip install pyinstaller
REPTYR_PATH=$(command -v reptyr) ./packaging/build-binary.sh
```

产物位于 `dist/pman-linux-amd64`、`dist/pman-linux-arm64` 或 `dist/pman-linux-armv7`，复制到目标机后即可直接
运行：

```bash
install -m 0755 dist/pman-linux-amd64 /usr/local/bin/pman
pman doctor
```

二进制必须按 CPU 架构分别构建；当前脚本支持 amd64、arm64 和 armv7。单文件启动时会
把内置 helper 解压到临时目录，因此目标机的临时目录必须允许执行文件。

## TUI 操作

| 按键 | 功能 |
|---|---|
| `Tab` | 在 `MANAGED`、`USER`、`ALL` 三种视图间切换 |
| `/` / `c` | 按 PID、用户、名称或命令搜索 / 清空搜索 |
| `s` | 切换 CPU、内存、PID、名称排序 |
| `?` / `h` | 打开内置完整快捷键帮助页 |
| `i` | 接管选中且带 TTY 的外部进程并在后台继续 |
| `n` | 输入命令并启动任务 |
| `Enter` / `a` | 附着到任务，进入前台交互 |
| `Ctrl-]` | 从前台脱离，任务转为后台运行 |
| `Ctrl-Z` | 从前台脱离并暂停任务 |
| `Space` | 暂停（SIGSTOP）或继续（SIGCONT） |
| `o` | 把后续输出切换到另一个日志文件 |
| `l` | 在 TUI 中查看日志 |
| `t` | 请求正常停止（SIGTERM） |
| `k` | 强制停止（SIGKILL，需要确认） |
| `d` | 移除已结束的任务记录；日志文件保留 |
| `q` | 退出 TUI；任务不受影响 |

视图说明：

- `MANAGED`：只显示已经由 pman 管理的任务；
- `USER`：默认视图，显示当前 Unix 用户的全部进程以及 pman 任务；
- `ALL`：显示 `/proc` 中可见的全部用户和系统进程，包括 systemd 服务与内核线程。

外部进程名称后带 `*`。带控制终端的外部进程可按 `i` 接管输出；没有 TTY 的服务
进程无法追溯重定向输出，但仍可用 `Space`、`t`、`k` 暂停、继续或结束。PID 1、
pman 自身及当前 TUI 的祖先进程会显示为受保护对象，不允许误操作。

## CLI 用法

启动并留在后台：

```bash
pman run -n web -l /var/tmp/web.log -- python3 -m http.server 8080
```

启动后立即附着到前台：

```bash
pman run --attach -- ./my-server --port 9000
```

知道暂停任务 PID 时，也可以不进 TUI 直接接管：

```bash
pman adopt -l /var/tmp/job.log PID
```

也可以直接向尚未接管的进程发信号：

```bash
pman signal-pid PID stop
pman signal-pid PID cont
pman signal-pid PID term
```

管理任务（ID 可以只写不含歧义的前几位，也可以使用唯一名称）：

```bash
pman list
pman attach web
pman pause web
pman resume web
pman redirect web /var/tmp/web-new.log
pman logs -n 200 web
pman stop web
```

`pman doctor` 会显示守护进程状态、socket、状态文件和默认日志目录。
客户端会检查 daemon 协议版本；旧 daemon 没有活动任务时会自动安全重启升级，避免
新界面连接旧后台后出现 `unknown command`。

## 文件位置

- 状态：`~/.local/state/pman/state.json`
- 日志：`~/.local/state/pman/logs/`
- 守护进程诊断：`~/.local/state/pman/daemon.log`
- socket：优先使用 `$XDG_RUNTIME_DIR/pman.sock`

设置 `PMAN_HOME=/some/path` 可以把上述运行数据隔离到指定目录，适合测试。

## 边界与安全

- TUI 扫描系统 `/proc`，但接管必须由用户按 `i`、`o` 或 `Enter` 明确触发。
- 接管要求目标属于当前用户、有控制终端，并且系统 ptrace 策略允许。用
  `pman doctor` 检查 `reptyr` 与 `ptrace_scope`。
- 某些复杂 pipeline 或共享同一进程组的大型任务可能被 reptyr 拒绝；失败时目标会
  保持暂停，不会被 pman 杀掉。
- 暂停、继续和停止信号发送给整个进程组，所以常见的父子进程任务能一起管理。
- 切换日志只影响之后的输出；旧日志不会被删除。
- TUI 的实时查看者如果长时间不读取，可能跳过屏幕输出，但完整数据仍以日志为准。
- 守护进程与 socket 都属于当前 Unix 用户；不要用 root 运行不可信命令。
