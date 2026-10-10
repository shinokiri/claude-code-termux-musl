# 设备验证：2026-10-10

这是一次无真实 Claude 账号的本机验证。结论是：musl 路线已跑通 Claude Code 的核心
Agent 工具链，可以继续在此基础上做 Termux 适配。真实认证、模型服务和长会话仍待验证。

## 环境

| 项目 | 实测值 |
| --- | --- |
| 设备 | Samsung SM-S9180 / Galaxy S23 Ultra |
| Android | 16，API 36 |
| CPU / 页大小 | aarch64 / 4096 bytes |
| 内核 | 5.15.189-android13-8-3251900-abS9180ZCS8FZH3 |
| termux-exec | 1:2.5.0-1 |
| Clang | 21.1.8 |
| Python / Node | 3.14 / 26.4.0 |
| Claude Code | 官方 `linux-arm64-musl` 2.1.296 |
| musl | Alpine 1.2.6-r2 |
| 运行方式 | 直接执行修改解释器路径后的 ELF；无 PRoot、glibc 或容器 |

## 通过的检查

| 检查 | 结果与范围 |
| --- | --- |
| musl 加载器、Claude 启动 | `--version`、`--help`、`--init-only` 均为退出码 0 |
| Bash | shell 命令、GNU grep/find、Git 正常 |
| Python / Node 脚本 | `#!/usr/bin/env` 脚本分别返回 `ENV_PY_OK` / `ENV_NODE_OK` |
| Read、Write、Edit | 真正读写测试文件，并核对修改后的文件内容 |
| Grep、Glob | 官方内置工具返回预期文件及内容；保留内置 ripgrep |
| 内置程序再次调用 | 分别以 `rg`、`ugrep`、`bfs` 为 argv[0] 通过 dispatch 启动成功 |
| SessionStart Hook | `#!/usr/bin/env python3` 脚本创建指定标记文件 |
| stdio MCP | 脚本服务器完成 initialize、tools/list、tools/call，返回 `MCP_OK` |
| Claude 的 HTTP 代理链路 | 真实 Claude 请求本地 API stub；代理连接日志记录全部 8 次请求 |
| Android DNS / CONNECT / TLS | 经 Bionic 代理读取官方 npm HTTPS metadata，证书校验开启 |
| 代理认证与生命周期 | 缺少 token 返回 407；token 权限 0600；父进程管道关闭后退出 |
| 更新拒绝与回退 | 3 个回归用例通过：损坏下载拒绝、初始化失败保留旧版、更新后回退 |
| TUI 显示 | 在有控制终端的 PTY 中显示版本、欢迎界面和主题选择 |

工具验证是本地 API stub 驱动真实 Claude Code 执行工具，未产生真实模型请求。
CLI 输出中的 token/cost 数字来自测试响应，不能解读为实际用量或费用。
测试使用显式允许的有限工具清单，未启用 `--dangerously-skip-permissions`。

## 实际解决的问题

1. musl ELF 原本指向不存在的 `/lib/ld-musl-aarch64.so.1`，现改为仓库私有加载器路径。
2. Bionic 的 termux-exec preload 无法供 musl 使用；清除它后，Termux 脚本又丢失
   `/usr/bin/env` 路径重写。启动器在 musl 入口清除，在 Shell/MCP/Hook 入口恢复。
   对照测试中，没有桥接时同一 Python 脚本退出 126，并报解释器不存在；桥接后通过。
3. 最新 Termux patchelf 包依赖本机 libc++ 没有的符号。改为固定源码在本机编译，
   没有升级用户的系统包。
4. Android 本机禁止硬链接，更新备份改为复制并重命名。

## 已知问题与未覆盖范围

**首次设置界面的退出需要第三次 Ctrl-C。** 实测两次 Ctrl-C 后界面退出，进程仍存活；
在正确绑定控制终端的 PTY 中再按一次 Ctrl-C，进程以退出码 0 结束。初次测试中没有
绑定控制终端时，第三次按键不能产生终端 SIGINT，因此也无法证明真实终端上的行为。
最终测试已使用 `TIOCSCTTY` 绑定控制终端，确认第三次按键可退出。

`tests/tui-smoke.py` 保留“两次按键应退出”的检查，当前退出码为 1，以便以后升级时
发现这个行为是否修复。没有为了此现象禁用官方功能，也尚未确认问题属于上游还是平台。

尚未验证真实账号 OAuth、付费推理、长时间任务、网络切换、后台会话、Remote Control、
完整 Tool Search、图片输入以及 Android 上的 Linux 级沙箱能力。命令 Hook 的验证不代表
所有 exec-form Hook 均受 `CLAUDE_CODE_SHELL_PREFIX` 包装。测试覆盖范围内的 Bash
使用 Termux GNU 命令；自定义 shell alias/function 的传递还没有专门验证。

## 本机原始记录

以下文件位于被 Git 忽略的 `.runtime/` 中，便于复查，不作为发布内容：

- `.runtime/runtime.json`：版本、下载地址、完整性校验值、原始和修改后 SHA-256。
- `.runtime/tests/smoke-jtyuo53i/report.json`：最终带代理的工具测试；8 次 API stub 调用，
  全部工具通过，退出码 0，约 9.29 秒。
- `.runtime/tests/smoke-jtyuo53i/proxy.log`：与上述测试对应的代理连接记录。
- `.runtime/proxy-smoke.json`：DNS、TLS、认证拒绝、清理检查。
- `.runtime/tui-smoke.json` 和 `.runtime/tests/tui-61smk33q/`：TUI 与三次 Ctrl-C 记录。

上述耗时只描述这次本地测试，不能外推为模型响应速度或设备间性能比较。
