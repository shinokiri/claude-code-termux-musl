# Claude Code：Termux musl 适配

基于 [Aarstad/claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)
的设备适配分支，基线为 `efd767cacdfdfccbfc94bc5b5628149f0d44a4d3`。
使用 Anthropic 官方 `linux-arm64-musl` 二进制，在 Termux 中直接执行。

**当前状态：Claude Code 2.1.296 已在 Samsung S23 Ultra / Android 16 上跑通核心工具。**
Bash、Read、Write、Edit、Grep、Glob、脚本 Hook 和 stdio MCP 均通过本地模拟 API
驱动的真实 CLI 测试。尚未验证真实账号登录、模型推理和长时间会话；首次设置界面的
Ctrl-C 退出需要第三次按键，见[设备验证记录](docs/device-validation.md)。

## 安装与启动

先确认依赖：ARM64 Termux、Python 3.11+、Clang/Clang++、Git、Bash、termux-exec 和 CA 证书。
在缺少依赖的 Termux 环境中可使用：

```bash
pkg install python clang git bash termux-exec ca-certificates
git clone https://github.com/shinokiri/claude-code-termux-musl.git
cd claude-code-termux-musl
python3 tools/prepare-runtime.py --version 2.1.296
python3 tools/build-bridges.py
python3 tools/install-command.py
```

最后一步将启动器链接为 `~/.local/bin/claude`，已有同名程序时会停止，不会覆盖。
如果该目录尚未加入 PATH，将下面这行放进 shell 的启动文件，并在当前终端执行一次：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

之后可以在任意项目目录直接启动，它会保留当前工作目录：

```bash
claude --version
claude auth login
claude
```

程序、musl 加载器、适配组件、下载缓存和默认 Claude 配置保存在仓库的 `.runtime/`
目录中。默认 `CLAUDE_CONFIG_DIR` 为 `.runtime/config`；显式设置该变量时尊重已有设置。
正常运行保留用户的 `HOME`，因此工具仍能访问普通 Termux 项目和工具链。
`.runtime/` 已加入 Git 忽略，登录后不要复制或公开其中的配置与凭据。

命令入口链接到本地检出目录，运行文件也位于其中；保留这个目录以便启动及继续开发。
GitHub 保存源码、文档和测试，运行文件在每台设备上下载及编译。

## 从源码准备运行环境

测试还使用 Node、curl、grep、find。仅运行 `python3 tools/prepare-runtime.py` 和
`python3 tools/build-bridges.py` 也可以保留私有安装，通过 `./bin/claude-termux` 启动。

安装器从 npm 下载官方包并验证 registry 提供的 SHA-512 integrity；从 Alpine 下载
musl 并验证固定 SHA-256。它只提取指定的普通文件，修改 ELF 的解释器路径，记录原始
及修改后的哈希。patchelf 从固定提交本地编译，以匹配设备已安装的 libc++。

初次安装下载约 95 MB，运行目录约 250 MB。保留旧版和更新时的临时副本会额外占用空间，
更新前建议留出约 1 GB。运行目录的绝对路径会写入 ELF 和桥接程序；移动仓库后需要重新
执行以上两个准备命令。

## 本分支的适配

- `claude-dispatch.c` 是 Bionic 启动程序，进入 musl 前清除不兼容的 `LD_PRELOAD`
  和 `LD_LIBRARY_PATH`，保留 `argv[0]`，支持官方内置 `rg`、`ugrep`、`bfs` 的再次调用。
- `termux-shell.c` 通过官方 `CLAUDE_CODE_SHELL_PREFIX` 接口包装 Bash、命令 Hook
  和 stdio MCP；执行 Termux 程序时恢复 termux-exec。解决 `#!/usr/bin/env python3`
  和 Node 脚本找不到解释器的问题。
- 复用上游 C 代理，让 Android/Bionic 处理 DNS。默认每个 Claude 会话使用独立随机
  端口和认证 token，随主进程退出；保留已有 HTTP/HTTPS 代理设置。HTTP(S) 类型的
  `ALL_PROXY` 会映射到 HTTP/HTTPS 变量，仅设置 SOCKS 类型 `ALL_PROXY` 时明确报错。
- 启动器通过 `exec` 交接进程，保留终端控制和信号行为；不会在终端 Ctrl-C 外再转发一次。
- 官方自动更新关闭。更新由本仓库校验、启动验证、备份与替换，防止换入不适用的运行文件。

普通启动不会开启测试用模拟 API，不会设置假密钥，不会禁用 Tool Search、WebSocket
或 MCP 功能。测试脚本为覆盖 MCP 固定工具，单独设置 `ENABLE_TOOL_SEARCH=false`。

## 更新与回退

显式指定需要测试的版本：

```bash
python3 tools/prepare-runtime.py --version 2.1.296
python3 tests/device-smoke.py --with-proxy
```

下载校验、新二进制 `--version`、`--help`、`--init-only` 全部成功后才替换当前版本。
替换前复制保存一个旧版本，避免 Android 对硬链接的限制。工具功能测试应在每次升级后运行，
初始化检查本身不能证明全部功能兼容。安装器使用文件锁，防止并发更新。

```bash
python3 tools/prepare-runtime.py --rollback
```

只有发生过成功的版本替换后才存在回退目标。请使用本分支的更新工具；仓库保留的上游
`install.sh`、`bin/claude-musl` 和 `bin/claude-musl-update` 是另一套全局安装入口，
不包含这里的设备适配。

## 验证

```bash
python3 tests/device-smoke.py --with-proxy
python3 tests/proxy-smoke.py
python3 tests/test-runtime-update.py
python3 tests/tui-smoke.py
```

工具测试使用 localhost API stub 与假测试密钥，只让真实 Claude Code 执行受控的本地
工具操作，不访问真实模型服务。代理测试通过 npm HTTPS metadata 检查 CONNECT、DNS、
TLS、认证拒绝和退出清理。TUI 测试是诊断项，目前会报告“两次 Ctrl-C 退出”检查失败，
同时记录第三次按键可以正常退出。

诊断日志默认不启用。需要检查本地代理时，可设置
`CLAUDE_TERMUX_PROXY_LOG=/absolute/path/proxy.log`，日志包含连接目的地址，不记录请求内容。
`CLAUDE_MUSL_NO_PROXY=1` 可关闭自动创建的代理，适合自行管理网络的测试环境。

## 来源与许可

适配代码沿用上游 MIT 许可，保留 [LICENSE](LICENSE)。[原上游 README](docs/upstream-README.md)
用于说明历史实现，其中设备性能数字和旧安装命令不代表本分支的验证结果。

- [Anthropic 官方 npm 包](https://www.npmjs.com/package/@anthropic-ai/claude-code-linux-arm64-musl)
  在安装时下载；不在仓库中分发 Claude Code 程序。
- [官方环境变量文档](https://code.claude.com/docs/en/env-vars)说明 Shell、前缀包装和代理设置。
- musl 使用 Alpine `v3.24/main/aarch64/musl-1.2.6-r2.apk`；固定摘要最初交叉参考
  [Termux Muscle 的兼容性记录](https://github.com/octocore-autonomous-systems/termux-muscle/blob/5a0cb2cf787e00e6be9187005df42c4b69695a05/compatibility.json)，
  并在本机对官方下载内容进行哈希校验。这不是独立的 Alpine 签名验证。
- [patchelf](https://github.com/NixOS/patchelf/tree/382d8bc12db9e720515da49d9c272ef4a622c3c0)
  在 `.runtime/tools/patchelf-src` 保留原项目源码与许可。
