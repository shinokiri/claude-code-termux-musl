# Claude Code：Termux musl 适配

基于 [Aarstad/claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)
的设备适配分支，基线为 `efd767cacdfdfccbfc94bc5b5628149f0d44a4d3`。
使用 Anthropic 官方 `linux-arm64-musl` 二进制，在 Termux 中直接执行，无需 PRoot 或 glibc。

**适配器 1.7.7 默认使用进程内执行与 Android DNS 兼容层。** ELF 自动加载兼容库，
Claude 自启动和 Termux 工具之间的运行库切换由原生层完成。日常运行无需 Shell/进程包装器、
本地 HTTP 代理或 JavaScript API 补丁。详见[执行架构与边界](docs/native-execution.md)。

可选的 `optional-off` 隐私模式在程序启动前停用已确认的可选上报组件，覆盖事件队列、
OTLP 导出器、内置插件遥测的收集与排队及已知请求出口。1.5 还将共享项目采集器缩减为
Android／iOS 项目识别，停止为遥测额外读取软件源、密钥管理器和 CI 配置。
1.6 继续停用 Git 仓库分类、项目文件数量、权限和辅助功能的专用分析采集入口；
共享的 Git、文件搜索、权限判断和屏幕阅读器功能保留。见[专用采集入口控制](docs/collector-privacy.md)。
1.7 关闭模型请求的缓存诊断选项，保留提示缓存与工具执行；同时处理自定义配置重新加入
缓存诊断的路径。1.7.1 恢复工具耗时的官方行为，由原程序及其官方开关决定是否附加。
1.7.2 另行停用 artifact 预览的专用事件发送入口，覆盖其公开转导出，并保留共用 artifact
客户端及预览界面选择；已知直连与 relay 上报路径还有请求层兜底。
见[工具耗时的取舍与对照](docs/tool-timing.md)及 [1.7.0 请求诊断记录](docs/request-privacy.md)。
此模式会加载 JavaScript 控制层，
默认不启用，不能保证所有信息零外发。
它单独处理具体上报路径，不设置会连带影响其他功能的遥测总开关；保留这些共享开关的原有行为，
不等于保留可选上报。
详见[隐私控制、验证范围与更新约束](docs/privacy.md)。
当前实现各自处理到哪一层、业务请求附加信息与共享采集器的对照证据，见
[架构与数据流审计](docs/architecture-review.md)。

1.7.7 支持官方 Claude **2.1.303 / Bun 1.4.4**，并修复运行期间重新应用配置可能带回
可选诊断字段的问题。两种官方环境配置入口返回前立即恢复隐私设置，工具耗时及其他业务
参数保留。档案扩展到 43 个模块、保护修订号 7，同时保留 2.1.301 和 2.1.296 的对应档案。
1.7.6 在实际安装验收中暴露此问题，已隔离为预发布；安装器拒绝了失败候选并保留旧版。
见[配置重载复核记录](docs/audits/2.1.303-settings-reload.json)和
[2.1.303 静态复核记录](docs/audits/2.1.303-privacy-update.json)。

**Claude Code 2.1.303 已在 Samsung S23 Ultra / Android 16 上通过核心工具和后台启动测试。**
测试使用本地模拟 API 驱动真实 CLI，覆盖 Bash、Read、Write、Edit、Grep、Glob、脚本 Hook
和 stdio MCP。真实账号登录、付费模型服务和长时间任务仍待验证；首次设置界面需要第三次
Ctrl-C 才退出。范围及原始记录见[设备验证](docs/device-validation.md)。

## 安装与启动

支持 Android 10+ / ARM64 Termux、Python 3.11+，目前实测设备为 Android 16。
安装和升级会编译适配组件并执行工具验收，需要以下依赖：

1.7.3 的合成验收还要求内核允许 seccomp 用户通知、`pidfd_getfd` 和读取自有进程信息。
安装前会实际检查这些能力；仅满足 Android 版本号并不保证具备。检查失败会明确拒绝新安装／
更新并保留现有版本，不回退到无保护验收。日常 Claude 会话不加载该测试保护器。

```bash
pkg install python clang git bash termux-exec ca-certificates nodejs curl grep findutils openssl
git clone https://github.com/shinokiri/claude-code-termux-musl.git
cd claude-code-termux-musl
./install.sh
```

安装器下载官方包、校验摘要、编译组件并测试候选版本。全部通过后，生成独立安装和命令入口。
已有其他程序占用同名入口时会停止；从本分支旧版链接到当前检出目录的入口可自动迁移。
已有 Claude 配置可显式指定，例如 `./install.sh --config-dir "$HOME/.claude"`，
或设置 `CLAUDE_CONFIG_DIR`。安装器保留其他配置和自定义包装器，只移除本适配器旧版写入的包装器设置。

```bash
claude --version
claude auth login
claude
```

入口默认安装到 `~/.local/bin/claude`，保留调用时的工作目录。若 `~/.local/bin` 已在 PATH 中，
可以直接使用以上命令，无须再修改 PATH。否则用绝对路径 `~/.local/bin/claude`，或将该目录
加入 PATH；也可以安装时通过 `--bin-dir` 选择已有 PATH 目录。

默认安装官方 `latest`；也可用 `--channel stable` 或 `--version 2.1.303` 指定首次安装版本。

**安装完成后可以删除本地源码检出目录。** 日常启动、检查、更新 Claude 及适配代码和回退均使用
独立安装中的文件。运行文件包含安装位置的绝对路径，不能直接移动；更换位置时应重新安装。

## 文件位置

| 内容 | 默认位置 |
| --- | --- |
| 完整版本和适配代码 | `~/.local/share/claude-termux/releases/<版本>-<标识>/` |
| 当前版 / 尚未清理的上一版指针 | `~/.local/share/claude-termux/current`、`previous` |
| 已校验下载和构建工具缓存 | `~/.local/share/claude-termux/cache/` |
| 自动更新设置 / 最近结果 | `~/.local/share/claude-termux/update-settings.json`、`update-status.json` |
| 最近一次后台更新日志 | `~/.local/share/claude-termux/update.log` |
| 隐私模式，跨版本保留 | `~/.local/share/claude-termux/privacy-mode` |
| 配置、登录状态和会话 | `~/.config/claude-termux/claude/` |
| 命令入口 | `~/.local/bin/claude`、`claude-termux-manage` |

尊重 `XDG_DATA_HOME`、`XDG_CONFIG_HOME`，也支持 `--data-dir`、`--config-dir`、`--bin-dir`。
启动时显式设置的 `CLAUDE_CONFIG_DIR` 优先。切换版本不会回退或删除配置和凭据。普通运行保留
用户的 HOME，因此可以使用已有的 Termux 项目和工具链。不要公开配置目录。

首次下载约 95 MB，每个完整版本约 250 MB，另有下载和构建缓存。更新前建议保留约 1 GB
可用空间。**1.7.5 起只保留当前版和正在使用的旧版，不专门保留回退副本。**
`previous` 指针不再阻止清理；对应的旧版被删除时同时移除指针。Claude、后台任务、工具和
管理命令通过进程租约保护使用中的文件。安装事务结束并释放管理器租约后，由当前管理器
执行清理；旧版管理器迁移期间也会保留其收尾所需文件。
活跃交互会话的现有监视器会继续检查旧版占用，退出后再做一次本地清理；释放占用的旧版
在下一次清理时删除，不必等官方再次发版。后台子进程若更晚退出，可由后续会话或手动清理回收。
没有旧版时不增加定时清理进程；本地清理不发起网络请求，也不改变官方更新检查间隔。
1.3 及更早版本没有租约，无法据此确认占用，仍需人工检查；无法识别的目录也不自动删除。
清理不涉及用户配置、登录状态、项目或会话历史；结果保存在 `gc-status.json`。
可用 `claude-termux-manage gc` 预览，或 `claude-termux-manage gc --apply` 立即执行同一安全检查。

## 更新、检查与回退

**默认自动跟随官方 `latest`。** 在终端交互启动 `claude` 后，更新器在后台检查官方新版及本仓库
已发布的适配版本；同一安装默认每小时最多检查一次，长会话期间也会继续检查。下载、适配和
工具验收全部在后台完成，成功后下次启动使用新版本，当前会话继续使用原版本。检查失败或
新版验收不通过时保留工作版本，并记录原因。Android 暂停或结束 Termux 后，检查可能延后；
下一次正常启动会再次检查。不使用 Claude 时不会有常驻更新服务。

立刻检查并更新，不必查询或填写版本号：

```bash
claude update
# 等价入口：claude-termux-manage update
```

其他管理命令：

```bash
claude update --check                          # 只查询，不安装
claude update --channel stable                 # 改为跟随官方 stable
claude update --channel latest                 # 改回 latest
claude update --version 2.1.303                 # 明确指定并固定 Claude 版本
claude-termux-manage updates                   # 查看设置和最近更新结果
claude-termux-manage auto-update off            # 关闭后台更新
claude-termux-manage auto-update on             # 开启；下次交互启动生效
claude-termux-manage auto-update on --interval 3600
claude-termux-manage doctor
claude-termux-manage test
claude-termux-manage rollback
```

显式指定版本后，后台会保留这个 Claude 版本，仍可更新适配代码。再次执行 `claude update`
会解除固定并跟随所选渠道。切换 `latest` / `stable` 不自动降级；需要降级时指定确切版本。
`rollback` 成功后暂停后台更新，避免刚回退就再次升级；`claude update` 或 `auto-update on`
可解除暂停。手动关闭的自动更新不会因执行 `claude update` 而被重新开启。

更新把二进制、musl 加载器、musl/Bionic 原生兼容库和适配脚本一起放入新的版本目录，
先完成启动、原生执行、DNS、HTTPS/WSS 和工具验收，再原子切换 `current`。即使新程序能打印版本号，只要实际测试失败，也不会
替换工作版本。`rollback` 只在 `previous` 仍存在时可用，执行前同样检查文件摘要并重新测试工具。
默认清理会移除闲置的上一版，因此不保证保留本地回退目标。需要旧版时，可用
`claude update --version <版本号>` 重新下载安装；`optional-off` 仍要求该版本有已验证档案。

`doctor` 检查已安装文件的摘要、启动、命令链接和执行模式，不读取或输出密钥。
`test` 重新测试原生执行、DNS、HTTPS/WSS、证书校验、工具、后台启动和进程租约。
显式执行 `claude-termux-manage test --with-proxy` 才构建并检查备用代理。

Claude 直接来自 Anthropic 官方 npm musl 包，校验 SHA-512。默认 `standard` 模式无需等本仓库
针对每个 Claude 版本重新发布。**启用 `optional-off` 后，新版必须有匹配的隐私配置档案并通过
隐私验收，否则保留当前版。** 更新器先比较已验证档案；未知新版在下载和构建前标记为
`deferred`。跟随渠道时可以选取不超过渠道目标的最新已验证版本，明确固定版本时不擅自替换。
适配代码来自本仓库 GitHub 正式 Release，校验发布清单中的 SHA-256，
解包到临时目录并用其安装器构建候选版，完成后删除临时源码。两者由同一条更新命令管理。
1.4 起还必须通过本地固定 Ed25519 公钥验证 `update.json.sig`，签名覆盖清单原始字节及其中的
归档摘要、隐私档案索引。签名缺失、错误或来自未知密钥时拒绝本次更新，不降级为无签名安装。
首次从旧更新器升级到 1.4 仍依赖原有发布源信任；独立签名验证从安装了固定公钥的新更新器开始。
签名私钥不在仓库或发行包中。更新清单和下载地址固定到本仓库，不接受任意外部代码地址。

GitHub 查询失败时，仍可用现有适配代码尝试官方 Claude 更新，并明确报告适配检查未完成。
已经发现的新适配包若摘要错误或验收失败，则本次更新失败，不发布候选版。
所有安装操作共用文件锁；多个会话不会并发安装同一个更新。后台日志保留最近一次尝试。

官方内置更新流程仍关闭，`claude update` 由启动器接到上述 Termux 更新器。脚本模式 `-p`、
`--version`、`--help` 和 Claude 自启动的子进程不触发后台更新。环境变量
`CLAUDE_TERMUX_AUTO_UPDATE=0`、`DISABLE_AUTOUPDATER=1` 或 `DISABLE_UPDATES=1`
可为本次交互启动禁用后台检查。自动检查不会读取模型凭据或调用付费模型；升级验收使用本地
模拟 API。若上游改变了兼容接口，仍可能需要先发布适配修复，不能保证每个未来版本立即可用。

## 适配方式

- `tools/patch-runtime.py` 为已校验的官方 ELF 添加绝对路径 `DT_NEEDED`，加载
  `libtermux-musl-compat.so`。原有程序段的偏移和地址保持不变；未知布局直接拒绝安装。
  Claude/daemon/session 即使使用精简环境自启动，也会由 ELF 加载器加载兼容层。
- `termux-exec.c` 分别编译为 musl 和 Bionic 库，检查目标解释器，处理脚本、常见路径和
  loader 环境切换；保留 PID、参数和内置工具 argv[0]。默认不设置官方 Shell/进程包装接口。
- `termux-musl-dns.c` 将 libc `getaddrinfo` 接到 Android 已有的 `/dev/socket/dnsproxyd`。
  `termux-musl-resolver.c` 为 c-ares 提供进程内 socketpair，将其原始 DNS 请求交给系统
  `resnsend`。保留 c-ares 的记录解析、超时与取消，显式指定的 DNS 服务器也保留原始传输。
  没有 IP 监听端口、代理进程或硬编码公共 DNS。
- 公开 `claude` 入口仍负责配置和自动更新，然后用 exec 交接，不额外转发 Ctrl-C。
  `claude-process-wrapper` 仅为旧会话、跨版本回退和显式备用代理模式保留。
- 已有 HTTP/HTTPS 代理设置继续生效；HTTP(S) `ALL_PROXY` 映射为 HTTP/HTTPS 变量。
  仅配置 SOCKS 类型 `ALL_PROXY` 时明确报错。C 代理保留为显式诊断模式，首次需要时才在
  共享缓存构建并记录摘要，不再进入默认构建过程。

普通启动不会启用模拟 API、假密钥或测试权限，也不会禁用 Tool Search、WebSocket、MCP
或后台 worktree 保护。测试专用设置仅存在于临时配置中；后台工具测试使用临时 Git worktree。

Android 不通过普通 Linux 的 `/etc/resolv.conf` 向应用提供当前网络的 DNS。1.0 使用本地
HTTP CONNECT 代理绕过这个差异；1.1 直接在解析边界接入 Android 系统服务，HTTP、TLS 和
WebSocket 继续由官方运行时处理。它不替代手机的 VPN，不拦截 TLS，也不自动过滤遥测域名。

这里使用的是 AOSP DNS 服务的实现协议，**不是 Android 承诺稳定的 NDK ABI**。最低 Android
版本检查和候选版验收可以发现一部分不兼容，不能保证未来所有系统或 Bun 改动都无需维护。
解析机制见 [DNS 架构](docs/native-dns.md)，新执行机制、旧版迁移和未覆盖入口见
[执行架构](docs/native-execution.md)。原生拦截不是对任意静态程序或直接系统调用的通用保证。

## 开发与验收

从源码准备私有调试运行文件：

```bash
python3 tools/prepare-runtime.py --version 2.1.296
./bin/claude-termux --version
```

该调试路径使用仓库的 `build/runtime/`，不会替换独立安装；构建器拒绝写入已封存的安装目录。
独立安装的更新与回退请使用管理命令。

```bash
python3 tests/test-elf-patch.py
python3 tests/test-runtime-update.py
python3 tests/test-updater.py
python3 tests/test-managed-install.py
python3 tests/test-storage.py
python3 tests/test-release-trust.py
python3 tests/native-exec-smoke.py
python3 tests/dns-smoke.py
python3 tests/network-smoke.py
python3 tests/device-smoke.py
python3 tests/device-smoke.py --process-wrapper
python3 tests/device-smoke.py --background
python3 tests/process-wrapper-smoke.py
python3 tests/proxy-smoke.py
python3 tests/tui-smoke.py
```

工具测试使用 localhost API stub 和假测试密钥，真正运行官方 CLI 的工具但不请求真实模型。
1.7.3 将合成验收统一接到 `tests/fixture_runtime.py`：使用独立 HOME/XDG 目录、明确的
Termux 工具路径、空的 Git 全局配置，不继承代理、SSH agent、模型凭据及 Shell/运行时
启动钩子。内核通知保护器随 fork/exec 继承，检查网络目的地并拒绝真实 SSH 可执行程序入口。
这项限制只用于合成测试；日常 Git/SSH、官方插件市场自动准备和隐私策略保持原有用途。
最早的 `--version` / `--help` / `--init-only` 候选检查也受保护；新版管理器验证旧版回退目标时
同样在旧测试脚本外建立保护。直接执行上述合成测试命令也使用同一入口。

验收报告的 `fixture` 字段记录保护器摘要、拒绝计数及进程回收结果。测试主进程正常退出、
超时或控制进程被终止后，保护器回收属于本次测试的脱离终端子进程；不使用宽泛的 `pkill`。
被拒绝的 SSH／外部下载分支不计入功能成功验收。保护器仍是防止测试意外外连的措施，
有指针参数检查与执行之间的时间差，且未隔离全部文件、本地 IPC 或系统服务；不宣称为
恶意代码沙箱或日常全进程隐私保证。保护器自身的接收端正负对照及清理检查可运行
`python3 tests/test-fixture-runtime.py`。显式的真实网络代理诊断 `proxy-smoke.py` 不属于合成验收。

后台测试另用临时 `apiKeyHelper` 提供测试密钥，不修改真实登录配置。测试记录它自己创建的
进程 PID 及启动时间，只清理这些进程。独立进程测试验证参数、PID、环境、多代直接自启动，以及
父进程退出后子进程的 HTTP 和 DNS 记录查询。DNS 与 TLS 检查使用受控的本地服务，测试
preload 只驱动验收，不作为普通启动的实现；TLS 测试临时生成证书，不更改系统信任库。

`tests/tui-smoke.py` 是诊断项，目前会报告“两次 Ctrl-C 退出”失败，并记录第三次按键可正常
退出；这一已知交互问题没有被隐藏为验收成功。完整测试报告保存在对应版本的 `acceptance.json`
及 `runtime/tests/`，测试日志不包含用户真实密钥。

默认无需设置 DNS 模式。需要诊断或临时退回旧连接方式时，可显式执行：

```bash
CLAUDE_TERMUX_DNS_MODE=http-proxy claude
```

备用模式仍使用 token 认证，每个 Claude 进程各自创建代理，父进程退出时清理；它不提供
原生模式的 c-ares 记录查询适配。`CLAUDE_TERMUX_PROXY_LOG=/absolute/path/proxy.log`
仅在此模式记录连接目的地址，不记录请求内容；`CLAUDE_MUSL_NO_PROXY=1` 阻止创建备用代理，
不会禁用默认的原生 DNS 兼容库。不会因原生解析失败而静默切换模式。

## 来源与许可

适配代码沿用上游 MIT 许可，保留 [LICENSE](LICENSE)。[原上游 README](docs/upstream-README.md)
是历史参考，其中安装命令、目录和性能数字不代表本分支。旧的全局安装、更新和卸载入口已移除，
`install.sh` 统一使用独立安装流程。

- [Anthropic 官方 npm 包](https://www.npmjs.com/package/@anthropic-ai/claude-code-linux-arm64-musl)
  由安装器下载，验证 registry 提供的 SHA-512 integrity；仓库不分发 Claude Code 程序。
  修改 ELF 解释器和动态依赖元数据，保留原有程序段内容；不修改 Claude Code 的内部 JavaScript。
- [Termux exec](https://github.com/termux/termux-exec-package) 继续用于 Bionic 工具的平台执行支持。
  [官方进程包装文档](https://code.claude.com/docs/en/corporate-launcher)描述保留的旧版兼容接口。
- musl 来自 Alpine `v3.24/main/aarch64/musl-1.2.6-r2.apk`，校验固定 SHA-256；固定摘要最初
  交叉参考 [Termux Muscle 的兼容性记录](https://github.com/octocore-autonomous-systems/termux-muscle/blob/5a0cb2cf787e00e6be9187005df42c4b69695a05/compatibility.json)，
  并在本机校验官方下载内容。这不是独立的 Alpine 签名验证。
- musl 编译头文件来自同版本 `musl-dev`，使用固定 SHA-256 校验；不混用 Bionic 头文件。
- [patchelf 固定提交](https://github.com/NixOS/patchelf/tree/382d8bc12db9e720515da49d9c272ef4a622c3c0)
  在设备上编译，匹配已安装的 libc++，不升级系统包；源码和原许可保留在安装缓存中。
