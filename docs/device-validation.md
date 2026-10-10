# 设备验证：2026-10-10

这是一次无真实 Claude 账号的本机验证。结论是：musl 路线已跑通 Claude Code 的核心
Agent 工具链，包括真实的按需守护进程和后台会话。真实认证、模型服务和长会话仍待验证。

## 1.2.0 原生执行验证

默认 Shell/进程包装被 musl/Bionic 原生库接替；下表测试在不设置这两个包装接口的模式下
执行。历史 1.1 的“移除包装即失败”对照不再代表新架构的结果。

| 检查 | 结果 |
| --- | --- |
| ELF 自动加载 | 原有程序段不移动；直接自启动和精简环境下三代子进程均加载兼容库 |
| 进程接口 | 5 种 Bun/Node API × 9 个场景，另加 8 个 Bionic libc exec 入口，共 53 项通过 |
| 脚本和路径 | 直接脚本、相对路径、符号链接、PATH、env shebang、env -S、常见绝对路径映射通过 |
| 进程语义 | PID、空/空格/Unicode 参数、2,000 个参数、stdin、额外 fd、cwd、SIGTERM、错误码通过 |
| 内置工具 | ugrep/bfs argv[0] 保持，真实 CLI 的 Grep/Glob 正常 |
| 工具链 | Bash、Read、Write、Edit、Grep、Glob、命令 Hook、stdio MCP；Bash 内直接再次启动 Claude 成功 |
| 后台服务 | 真实 daemon/session 加载原生库，未设置 Shell/进程包装；工具链成功，测试进程全部清理 |
| 脱离父进程 | 直接启动的 musl 子进程在父进程退出后完成 HTTP 和 TXT 查询 |
| DNS 和 TLS 回归 | 下述 DNS/Worker/并发/大响应、HTTPS/WSS、错误主机名证书检查继续通过 |
| 旧入口和备用代理 | 旧进程包装入口仍可启动新版；显式代理的前台工具、认证与生命周期检查通过 |
| TUI | 正常显示；两次 Ctrl-C 退出检查仍失败，第三次可退出，未将此项记为通过 |

原生加载与 ELF 格式的约束、未覆盖的系统调用路径及测试解释见[执行架构](native-execution.md)。
版本验收报告新增 `native_execution.json`，安装器封存两份原生库并检查其摘要。配置迁移只
更改本适配器拥有的包装器值，不改变用户的遥测、错误上报、反馈或调查设置。

## 1.1.0 原生 DNS 验证

本轮将默认 HTTP CONNECT 代理替换为进程内 DNS 库，并补齐之前仅拦截 `getaddrinfo`
没有覆盖的 c-ares 入口。以下是新的默认模式验证；后文代理及早期报告属于 1.0 的历史记录。

| 检查 | 结果 |
| --- | --- |
| DNS API | 12 类记录 × 5 种入口通过；独立 Resolver、命名导入、TTL、反向解析、Bun DNS 通过 |
| Worker | Bun / Node 两种 Worker，各验证继承环境和显式空环境，共 4 种情况 |
| 异常和并发 | NXDOMAIN、无记录、SERVFAIL、REFUSED、超时、取消、64 个并发请求通过 |
| 大响应 | 48 条 TXT（约 12 KB）触发 c-ares TCP 重试；24 个并发大响应通过 |
| 显式 DNS | `setServers` 及配合 `setLocalAddress` 的独立 Resolver 保留原始 UDP 服务器路径 |
| HTTPS / WSS | fetch、Node HTTPS、WSS 成功；Host/SNI 保持原域名；错误主机名证书被拒绝 |
| 子进程独立性 | 真实 musl 子进程保留 PID、参数和环境；父进程退出后 HTTP 与 TXT 查询成功，无代理进程 |
| 执行包装对照 | 直接 Shell 执行 env shebang 失败，包装后通过；直接 Claude 自启动缺少 DNS 库，包装后正确加载 |
| 系统 DNS | 同机真实 Android 系统服务的地址、TXT 和独立 Resolver 查询成功 |
| 回归 | 3 个运行文件更新、25 个自动更新和 8 个独立安装用例通过，包括 DNS 库摘要保护 |

受控 DNS 测试使用同一官方二进制的原生解析器作对照；生产启动不带 JavaScript preload。
原生库的机制和限制见 [DNS 架构](native-dns.md)。完整安装验收还会重新运行前台、自启动、
真实后台 daemon 的核心工具链，任何一项失败都不会切换当前安装。

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
| 运行方式 | 直接执行修改解释器及动态依赖元数据后的 ELF；无 PRoot、glibc 或容器 |

## 1.0 历史检查

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
| 独立安装与完整版本切换 | 重新下载官方包，在独立目录安装两套完整版本，管理命令检查和回退成功 |
| 升级前工具检查 | 人为破坏候选版 Shell 桥接；启动检查成功，但工具验收拒绝发布，当前及上一版指针不变 |
| 独立安装回归 | 7 个用例：拒绝坏候选、整套切换、拒绝外部链接、保护其他命令、保留配置并检测包装器冲突、损坏摘要拒绝、刷新验收报告不误报文件损坏 |
| 自动更新回归 | 25 个用例：官方渠道发现、整套升级与无变化跳过、失败保留工作版、重试间隔、适配源离线时继续官方更新、查询不修改安装、固定版本、回退暂停、归档校验与安全解包、跨安装器锁继承、启动不等待网络及监视进程生命周期、发布源暂不可用时查询结果仍可解析 |
| 公开发布包升级 | 临时安装从测试身份 `0.0.0` 同时升级为官方 Claude 2.1.296 和已发布适配器 1.0.0；包含公开清单下载、归档摘要、继承安装锁、真实工具验收及整套切换；重复执行更新不会创建额外版本，临时源码已删除 |
| 交互会话自动更新 | 在有控制终端的真实 Claude 会话中完成两次后台检查；第二次通过让检查时间到期来触发，未重启 Claude；关闭该会话后监视进程自动退出 |
| 进程包装协议 | 保留 PID、空参数、带空格参数、Unicode 和环境；支持嵌套；父进程退出后子进程仍能联网，代理均清理 |
| 包装器中的真实 Claude | 无 Bionic 加载器变量、带失效父代理的环境下启动，全部核心工具、Hook 和 MCP 成功 |
| 真实后台 Claude | `--bg` → daemon → PTY host → session / standby 均经过包装器；在临时 Git worktree 中通过同一套核心工具、Hook 和 MCP |
| TUI 显示 | 在有控制终端的 PTY 中显示版本、欢迎界面和主题选择 |

工具验证是本地 API stub 驱动真实 Claude Code 执行工具，未产生真实模型请求。
CLI 输出中的 token/cost 数字来自测试响应，不能解读为实际用量或费用。
测试使用显式允许的有限工具清单，未启用 `--dangerously-skip-permissions`。

公开更新与交互检查的复现入口为 `tests/updater-device-smoke.py --report /tmp/update-report.json`。
它创建并删除独立临时安装，测试身份 `0.0.0` 仅用于保证触发升级，不能解读为验证了所有历史
Claude 版本之间的迁移。所有验收继续使用同一真实官方二进制和工具路径。

## 实际解决的问题

1. musl ELF 原本指向不存在的 `/lib/ld-musl-aarch64.so.1`，现改为对应版本的私有加载器路径。
2. Bionic 的 termux-exec preload 无法供 musl 使用；清除它后，Termux 脚本又丢失
   `/usr/bin/env` 路径重写。启动器在 musl 入口清除，在 Shell/MCP/Hook 入口恢复。
   对照测试中，没有桥接时同一 Python 脚本退出 126，并报解释器不存在；桥接后通过。
3. 最新 Termux patchelf 包依赖本机 libc++ 没有的符号。改为固定源码在本机编译，
   没有升级用户的系统包。
4. Android 本机禁止硬链接，更新备份改为复制并重命名。
5. 命令和运行文件原先依赖源码检出目录。独立安装现在保存每套适配代码及运行文件，用原子
   链接切换整套版本，用户配置放在稳定的独立目录。
6. 后台进程原先可能绕过前台启动器，或继承生命周期属于父进程的代理。1.0 使用官方进程
   包装接口为每个进程建立自己的代理；1.1 保留包装接口，为每个进程加载自己的 DNS 库。
7. 普通 Linux 的 DNS 配置方式与 Android 不同。1.1 将 libc 和 c-ares 两条路径接入系统
   解析器，移除默认代理以及子进程对父代理生命周期的依赖。

## 已知问题与未覆盖范围

**首次设置界面的退出需要第三次 Ctrl-C。** 实测两次 Ctrl-C 后界面退出，进程仍存活；
在正确绑定控制终端的 PTY 中再按一次 Ctrl-C，进程以退出码 0 结束。初次测试中没有
绑定控制终端时，第三次按键不能产生终端 SIGINT，因此也无法证明真实终端上的行为。
最终测试已使用 `TIOCSCTTY` 绑定控制终端，确认第三次按键可退出。

`tests/tui-smoke.py` 保留“两次按键应退出”的检查，当前退出码为 1，以便以后升级时
发现这个行为是否修复。没有为了此现象禁用官方功能，也尚未确认问题属于上游还是平台。

尚未验证真实账号 OAuth、付费推理、长时间任务、网络切换、后台长时间运行、Remote Control、
完整 Tool Search、图片输入以及 Android 上的 Linux 级沙箱能力。命令 Hook 的验证不代表
所有版本/形式的 Hook 都已验证；1.2 默认不再依赖 `CLAUDE_CODE_SHELL_PREFIX`。测试覆盖范围内的 Bash
使用 Termux GNU 命令；自定义 shell alias/function 的传递还没有专门验证。

## 1.0 历史原始记录

以下是开发阶段的原始记录路径，位于被 Git 忽略的 `.runtime/` 中，不作为发布内容；
删除源码检出目录时这些调试记录也会删除，正式安装的验收报告则保存在独立目录中：

- `.runtime/runtime.json`：版本、下载地址、完整性校验值、原始和修改后 SHA-256。
- `.runtime/tests/smoke-jtyuo53i/report.json`：最终带代理的工具测试；8 次 API stub 调用，
  全部工具通过，退出码 0，约 9.29 秒。
- `.runtime/tests/smoke-jtyuo53i/proxy.log`：与上述测试对应的代理连接记录。
- `.runtime/proxy-smoke.json`：DNS、TLS、认证拒绝、清理检查。
- `.runtime/tui-smoke.json` 和 `.runtime/tests/tui-61smk33q/`：TUI 与三次 Ctrl-C 记录。
- `.runtime/tests/smoke-kdsim9x1/report.json`：真实后台工具测试通过，约 14.36 秒；
  使用临时 `apiKeyHelper` 提供仅供本地 stub 的假密钥，后台隔离保护保持启用。
- `.runtime/managed-check/`：独立安装、完整版本切换和回退的测试安装；每个版本的
  `acceptance.json` 汇总其验收结果。

正式独立安装的报告位于 `~/.local/share/claude-termux/current/acceptance.json` 和
对应版本的 `runtime/tests/`，不会随源码检出目录一起删除。

上述耗时只描述这次本地测试，不能外推为模型响应速度或设备间性能比较。
