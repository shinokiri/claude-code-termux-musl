# Claude Code：Termux musl 适配

基于 [Aarstad/claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)
的设备适配分支，基线为 `efd767cacdfdfccbfc94bc5b5628149f0d44a4d3`。
使用 Anthropic 官方 `linux-arm64-musl` 二进制，在 Termux 中直接执行，无需 PRoot 或 glibc。

**Claude Code 2.1.296 已在 Samsung S23 Ultra / Android 16 上通过核心工具和后台启动测试。**
测试使用本地模拟 API 驱动真实 CLI，覆盖 Bash、Read、Write、Edit、Grep、Glob、脚本 Hook
和 stdio MCP。真实账号登录、付费模型服务和长时间任务仍待验证；首次设置界面需要第三次
Ctrl-C 才退出。范围及原始记录见[设备验证](docs/device-validation.md)。

## 安装与启动

支持 ARM64 Termux、Python 3.11+。安装和升级会编译适配组件并执行工具验收，需要以下依赖：

```bash
pkg install python clang git bash termux-exec ca-certificates nodejs curl grep findutils
git clone https://github.com/shinokiri/claude-code-termux-musl.git
cd claude-code-termux-musl
./install.sh --version 2.1.296
```

安装器下载官方包、校验摘要、编译组件并测试候选版本。全部通过后，生成独立安装和命令入口。
已有其他程序占用同名入口时会停止；从本分支旧版链接到当前检出目录的入口可自动迁移。
已有 Claude 配置可显式指定，例如 `./install.sh --config-dir "$HOME/.claude"`，
或设置 `CLAUDE_CONFIG_DIR`。安装器保留配置中的其他键；发现其他进程包装器时会停止并说明冲突。

```bash
claude --version
claude auth login
claude
```

入口默认安装到 `~/.local/bin/claude`，保留调用时的工作目录。若 `~/.local/bin` 已在 PATH 中，
可以直接使用以上命令，无须再修改 PATH。否则用绝对路径 `~/.local/bin/claude`，或将该目录
加入 PATH；也可以安装时通过 `--bin-dir` 选择已有 PATH 目录。

**安装完成后可以删除本地源码检出目录。** 日常启动、检查、更新 Claude 二进制和回退均使用
独立安装中的文件。运行文件包含安装位置的绝对路径，不能直接移动；更换位置时应重新安装。

## 文件位置

| 内容 | 默认位置 |
| --- | --- |
| 完整版本和适配代码 | `~/.local/share/claude-termux/releases/<版本>-<标识>/` |
| 当前版 / 上一版指针 | `~/.local/share/claude-termux/current`、`previous` |
| 已校验下载和构建工具缓存 | `~/.local/share/claude-termux/cache/` |
| 配置、登录状态和会话 | `~/.config/claude-termux/claude/` |
| 命令入口 | `~/.local/bin/claude`、`claude-termux-manage` |

尊重 `XDG_DATA_HOME`、`XDG_CONFIG_HOME`，也支持 `--data-dir`、`--config-dir`、`--bin-dir`。
启动时显式设置的 `CLAUDE_CONFIG_DIR` 优先。切换版本不会回退或删除配置和凭据。普通运行保留
用户的 HOME，因此可以使用已有的 Termux 项目和工具链。不要公开配置目录。

首次下载约 95 MB，每个完整版本约 250 MB，另有下载和构建缓存。更新前建议保留约 1 GB
可用空间。`previous` 指向上一版，但更早的版本目录也会保留，避免破坏仍在运行的旧会话；
退出相关会话后可以自行删除不再需要、且不被 `current` / `previous` 指向的版本目录。

## 更新、检查与回退

官方自动更新关闭，防止它替换已适配的运行文件。用以下命令显式指定要安装的官方版本：

```bash
claude-termux-manage update --version 2.1.296
claude-termux-manage doctor
claude-termux-manage test
claude-termux-manage rollback
```

更新把二进制、musl 加载器、Bionic 桥接程序和适配脚本一起放入新的版本目录，先完成启动及
工具验收，再原子切换 `current`。即使新程序能打印版本号，只要实际工具测试失败，也不会
替换工作版本。回退前同样检查文件摘要并重新测试工具。首次安装后没有回退目标，成功更新
一次后才有；也可以回退后再次执行回退，在两套完整版本之间切换。

`doctor` 检查已安装文件的摘要、启动、命令链接和持久化的进程包装设置，不读取或输出密钥。
`test` 重新测试工具、后台启动、代理 DNS、HTTPS 证书校验和认证拒绝。

这些命令更新的是 Claude Code 运行版本，沿用已安装的适配代码。需要更新适配代码时，重新
获取本仓库并运行 `./install.sh`，同样经过候选版本验收。安装和更新使用文件锁。

目前没有自动跟踪最新版本、定时升级或同时更新适配代码的命令；`update` 必须提供具体版本号，
不接受 `latest`。`claude update` 不走本适配器的验收和 ELF 修补流程，因此请使用上面的管理命令。

## 适配方式

- `claude-dispatch.c` 在 Bionic → musl 边界清除不兼容的 `LD_PRELOAD` 和
  `LD_LIBRARY_PATH`，保留 `argv[0]`，支持内置 `rg`、`ugrep`、`bfs` 的再次调用。
- `termux-shell.c` 通过官方 `CLAUDE_CODE_SHELL_PREFIX` 包装 Bash、命令 Hook 和
  stdio MCP，执行 Termux 程序时恢复 termux-exec，使 `#!/usr/bin/env python3` 和 Node
  脚本可以工作。此接口与逐个参数传递的进程包装接口不同。
- `claude-process-wrapper` 接入官方 `CLAUDE_CODE_PROCESS_WRAPPER`。前台启动和
  后台自启动共用适配逻辑，保留参数、进程 PID 和环境变量；进入 musl 时只清除上述不兼容
  的加载器变量。安装器将包装器的稳定路径写入用户配置的 `env`，供后台服务读取。
- 复用上游 C 代理，由 Android/Bionic 解析 DNS。每个 Claude 进程拥有独立随机端口，使用
  token 认证；代理随其父进程退出。子进程重新创建自己的代理，避免父进程退出后失去网络。
  已有 HTTP/HTTPS 代理设置继续生效；HTTP(S) `ALL_PROXY` 会映射为 HTTP/HTTPS 变量。
  仅配置 SOCKS 类型 `ALL_PROXY` 时明确报错。
- 启动器用 `exec` 交接进程，不额外转发一次 Ctrl-C。官方进程包装接口仍有覆盖范围限制，
  例如 Chrome 原生消息宿主和 `--worktree --tmux` 的首个启动进程，详见官方文档。

普通启动不会启用模拟 API、假密钥或测试权限，也不会禁用 Tool Search、WebSocket、MCP
或后台 worktree 保护。测试专用设置仅存在于临时配置中；后台工具测试使用临时 Git worktree。

这里的“DNS 代理”实际是运行在 `127.0.0.1` 上的 HTTP CONNECT 代理。官方 Linux musl 程序
无法直接获得 Android 系统解析服务；musl 通常读取 `/etc/resolv.conf`，而 Android 不使用
这个文件来向应用提供当前网络的 DNS。Bionic 代理通过 Android 的异步解析接口解析域名，
再建立连接；HTTPS 内容以加密隧道转发，不做 TLS 解密。已有 HTTP/HTTPS 代理时复用已有代理，
不再启动这层本地代理。它的作用是适配解析和连接，并不替代手机已有的 VPN。

## 开发与验收

从源码准备私有调试运行文件：

```bash
python3 tools/prepare-runtime.py --version 2.1.296
python3 tools/build-bridges.py
./bin/claude-termux --version
```

该调试路径使用仓库的 `.runtime/`，不会替换独立安装。独立安装的更新与回退请使用管理命令。

```bash
python3 tests/test-runtime-update.py
python3 tests/test-managed-install.py
python3 tests/device-smoke.py --with-proxy
python3 tests/device-smoke.py --with-proxy --process-wrapper
python3 tests/device-smoke.py --with-proxy --background
python3 tests/process-wrapper-smoke.py
python3 tests/proxy-smoke.py
python3 tests/tui-smoke.py
```

工具测试使用 localhost API stub 和假测试密钥，真正运行官方 CLI 的工具但不请求真实模型。
后台测试另用临时 `apiKeyHelper` 提供测试密钥，不修改真实登录配置。测试记录它自己创建的
进程 PID 及启动时间，只清理这些进程。独立进程测试验证参数、PID、环境、嵌套包装，以及
父进程退出后子进程网络和代理清理。

`tests/tui-smoke.py` 是诊断项，目前会报告“两次 Ctrl-C 退出”失败，并记录第三次按键可正常
退出；这一已知交互问题没有被隐藏为验收成功。完整测试报告保存在对应版本的 `acceptance.json`
及 `runtime/tests/`，测试日志不包含用户真实密钥。

代理诊断日志默认关闭。需要时可设置 `CLAUDE_TERMUX_PROXY_LOG=/absolute/path/proxy.log`，
仅记录连接目的地址等信息，不记录请求内容。`CLAUDE_MUSL_NO_PROXY=1` 可关闭自动代理。

## 来源与许可

适配代码沿用上游 MIT 许可，保留 [LICENSE](LICENSE)。[原上游 README](docs/upstream-README.md)
是历史参考，其中安装命令、目录和性能数字不代表本分支。旧的全局安装、更新和卸载入口已移除，
`install.sh` 统一使用独立安装流程。

- [Anthropic 官方 npm 包](https://www.npmjs.com/package/@anthropic-ai/claude-code-linux-arm64-musl)
  由安装器下载，验证 registry 提供的 SHA-512 integrity；仓库不分发 Claude Code 程序。
  只修改 ELF 解释器路径，不修改 Claude Code 内部程序。
- [官方进程包装文档](https://code.claude.com/docs/en/corporate-launcher)和
  [环境变量文档](https://code.claude.com/docs/en/env-vars)定义这里使用的适配接口。
- musl 来自 Alpine `v3.24/main/aarch64/musl-1.2.6-r2.apk`，校验固定 SHA-256；固定摘要最初
  交叉参考 [Termux Muscle 的兼容性记录](https://github.com/octocore-autonomous-systems/termux-muscle/blob/5a0cb2cf787e00e6be9187005df42c4b69695a05/compatibility.json)，
  并在本机校验官方下载内容。这不是独立的 Alpine 签名验证。
- [patchelf 固定提交](https://github.com/NixOS/patchelf/tree/382d8bc12db9e720515da49d9c272ef4a622c3c0)
  在设备上编译，匹配已安装的 libc++，不升级系统包；源码和原许可保留在安装缓存中。
