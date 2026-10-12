# 原生 Android DNS 与执行边界

1.2 默认结构是官方 musl 二进制和进程内执行/DNS 兼容层，ELF 动态依赖保证每个 Claude
进程都加载兼容库。它不创建本地 HTTP CONNECT 代理，不改写 Claude Code 的 JavaScript。
执行层及迁移细节见[原生执行架构](native-execution.md)。
## 两条解析路径

| 调用路径 | 兼容方式 |
| --- | --- |
| 普通网络连接的 libc `getaddrinfo` | 调用 Android 已有 DNS Unix socket 的 `getaddrinfo` 协议；转换 musl/Bionic 的标志和错误码 |
| `node:dns.resolve*`、独立 Resolver、Bun DNS 的 c-ares 路径 | 保留 c-ares 的查询、解析器和计时逻辑；将默认 DNS socket 接到 Android `resnsend` 原始查询协议 |
| `Resolver.setServers`、`dns.setServers` 显式服务器 | 保留调用方选择的服务器和原始 IP 传输，不强制改为系统 DNS |
| Bun / Node Worker | 在同一进程的原生层生效，包括显式空环境的 Worker，无须替换 Worker 构造器 |

c-ares 读取 `/etc/resolv.conf` 时得到进程内合成配置，其地址 `127.0.0.53:53` 是内部标记。
只对该目的地的连接替换为 Unix socketpair；没有进程在这个 IP/端口监听，也不向它发送网络
数据。显式选择同一保留地址也会进入系统解析路径，不能用于指定外部 DNS 服务。

每个活跃 c-ares socket 使用一条进程内转发线程，异步处理最多 128 个待处理查询。Android
返回的原始 DNS 响应交回 c-ares，保留其记录结构、TTL 与错误语义。大于 UDP 限制的响应触发
原有 TCP 重试；TCP 帧不会互相穿插。描述符关闭后线程及待处理请求清理。取消与超时由原有
c-ares API 对调用方生效；已提交给系统的查询可能仍会在 Android 解析器内部完成。

普通 HTTPS/WSS 仍由官方运行时建立连接、发送 SNI 并校验证书。兼容库没有 HTTP 头修改、
TLS 解密或 TLS 指纹伪装。移除 HTTP 代理会改变“是否经过代理”这一运行环境事实，不能据此
推导服务端的风控结果，也不能承诺跨版本的 TLS 指纹逐字节不变。

## 从 1.1 包装器到 1.2 原生执行

1.1 的对照测试证明：只移除包装器，会导致 env shebang 脚本失败，以及自启动子进程缺失
DNS 库。这说明相应职责必须被接替，并不能证明外部包装器本身不可替代。

1.2 用 ELF 动态依赖解决每个 Claude 进程加载兼容层的问题，用 musl/Bionic 双份执行库
解决两个方向的 ABI 切换。真实 CLI、后台服务和显式精简环境的三代自启动已通过检查，
默认路径不再使用旧包装器。保留的入口只服务于旧会话、回退以及显式备用代理模式。

## 验证与更新

安装/升级候选版必须通过以下本机检查后才能替换工作版本：

- 12 类记录（A、AAAA、ANY、CAA、CNAME、MX、NAPTR、NS、PTR、SOA、SRV、TXT），每类
  5 种 API 入口；命名 ESM 导入、TTL、反向解析、Bun DNS、4 种 Worker 情况。
- NXDOMAIN、无记录、SERVFAIL、REFUSED、超时、取消、64 个并发查询、分段 IPC 返回、
  48 条 TXT 的大响应以及 24 个并发大响应；显式服务器保持原始路径。
- HTTPS fetch、Node HTTPS、WSS、原始 Host/SNI，以及错误主机名证书被拒绝。
- 真实 CLI 的前台、直接自启动、旧兼容入口和后台 daemon 工具链，包括 Bash、文件工具、Hook 和 MCP。
- 真实 musl 子进程在父进程退出后仍可完成 HTTP 请求和 TXT 查询。

上述测试用本地模拟 DNS/API；另在本机真实 Android DNS 服务上验证了地址与 TXT 查询。
实际验收报告保存在安装版本的 `acceptance.json`、`dns.json`、`network.json` 和各工具报告中。
不同 Bun 版本的 TXT 分块和无记录错误可能有差异；测试使用同一二进制的显式 UDP DNS 作为
对照，确认兼容层保留该二进制本来的行为，不把 Node 文档中的行为硬写成补丁。

2026-10-11 对同一官方 2.1.296 ELF 的局部反汇编还确认：导出的 `uv_getaddrinfo`、
`uv_freeaddrinfo`、`uv_spawn` 会把各自固定名称交给共享的“不支持此 uv 函数”处理器。
这些是未实现的 API 占位入口，不能用其导出名称推断真实 DNS 或子进程调用经过了哪里。
这次新增结果核对了 98 条指令与原 ELF 的字节一致性，并核对了实际引用的提示常量；
它来自独立的 Ghidra 有界分析，时间早于下文记录的全量 REA 自动分析完成。
其定位、摘要和分析边界见[原生入口研究记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/uv-stubs.json)。
随后将运行时返回地址与原件指令对应，确认了两条解析路径实际进入兼容层的位置：

| 独立进程中的本地测试 | 原 ELF 中观察到的调用点 | 进入的兼容入口 |
| --- | --- | --- |
| HTTPS `fetch`、WSS | `0x1f77598`、`0x1f77684` | libc `getaddrinfo` |
| Node HTTPS | `0x1dea84c` | libc `getaddrinfo` |
| 完整 DNS 记录查询矩阵 | `0x2a9eff4` | 读取 `/etc/resolv.conf` 的 `fopen` |
| 同一 DNS 矩阵中的默认解析连接 | `0x2aaf558` | 到内部标记地址的 `connect` |

最后一项的调用指令地址是 `0x2aaf558`，指令为 `blr x8`；实际记录的返回地址是
`0x2aaf55c`。后续完整会话中的查询恢复了默认指针初始化：`0x2abf634` 取出包装函数
`0x2ac02e0`，`0x2abf63c` 将它写入解析器对象的 `+0x138`；调用前从同一偏移加载 `x8`。
包装函数在 `0x2ac02ec` 以 `b` 尾跳转到 `connect` 的 PLT `0x4d05ac0`，保留先前的
返回地址。这条默认静态路径与早期 87 次内部标记连接观察相符；实验没有动态采样 `x8`，
也没有穷举自定义 socket 回调。新增 48 条指令、192 字节已同时与原件和安装件核对。
扫描直接 `B/BL` 调用仍会漏掉函数指针调用点；普通联网与记录查询确有不同的底层入口。

此轮共核对了 80 个直接分支和 5 个运行时调用点周围实际解码出的 41 条指令；其中，静态
发现的 7 个 `getaddrinfo` 直接调用点只有 3 个在早期联网测试中被触发，后续定向测试已
触发全部 7 个，见下表。适配后 ELF 的
`.text` 与官方原件摘要完全相同，因此这些地址可以对应原始代码；该 ELF 为 `ET_EXEC`，
不能把 `dladdr` 的加载基址从这些返回地址再减一遍。地址只适用于已核对的 2.1.296 原件。

研究观察器只记录入口名称和代码地址，独立编译到实验目录。DNS 全矩阵、HTTPS/WSS 综合
测试及 4 个独立进程的 API 测试均通过；后一组避免把前一个 API 的 DNS 缓存误认为当前
API 的解析路径。Host、SNI、错误证书拒绝、显式 DNS 服务器及 Worker 的原有检查均保留。
观察器会影响时序，不属于日常适配包；这些结果也不能证明所有原生路径、TLS 指纹或上报
行为已被完整覆盖。可审查的记录见[DNS 调用点研究](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/dns-call-sites.json)。

在对应的 Termux 安装上，可从仓库根目录复现这组实验：

```sh
python3 research/native-2.1.296/dns-callsite/study.py --output build/dns-callsite-study
```

脚本要求已核对的 Claude 2.1.296、适配器 1.7.2、clang 及缓存的 musl 头文件 APK；可以用
`--root`、`--musl-headers` 指定位置。它先校验版本、原件标识、已安装文件摘要、代码段及
测试源文件，输出目录必须不存在。实验使用独立配置和本地 DNS/TLS 接收器，不调用账号或
模型服务；安装版本、默认隐私模式与设置不被修改。输出含实验路径，分享前应按需去除。
脚本复核已保存的 Ghidra 指令字节，不启动全量 REA。重新反汇编可使用同目录内的 Java
脚本与种子 TSV，对单独导入的原件项目执行 `-noanalysis` 和对应 `-postScript`；各脚本
只解码受限地址范围，不创建完整函数或开启自动分析。

同日的全量 REA 查询在桥启动前因 `Disk quota exceeded` 失败；工作目录当时位于约
981 MiB、启用用户配额的 `/tmp` tmpfs。错误响应与证据包已保存，原临时目录由 REA 的
失败处理自动回收，失败前留存的快照及独立局部项目仍保留。随后仅将新会话的 `TMPDIR`
移到 ext4 数据目录，保持目标、工具源码和分析配置相同。16:01–16:07 UTC，按用户要求，
在不重启现有进程的情况下取消了本轮 REA 启动计时器，以及中转、连接与外层等待期限；
默认分析配置保持原样，运行时的等待监督行为已另行记录，临时调试端口已关闭。

随后按用户要求回收研究盘空间：校验后删除 3 个已解压使用的安装包，保留其下载地址和摘要；
13 份历史报告和旧快照中的 1 个缓存保留为 gzip，每份均在移除原始副本前核对解压内容的
SHA-256。共释放 2.06 GiB，已安装工具、当前工作目录、原始目标与交换文件均保留。
16:27 UTC 将正在运行的磁盘监控替换为冻结保护：每 5 秒检查，余量低于 1.5 GiB 时冻结
分析服务的 cgroup，达到 2.5 GiB 后恢复同一组进程；原先低于 512 MiB 时终止进程的逻辑
已移除。独立进程通过 8 项检查，包括暂停、阈值回差、同一进程恢复，以及保留外部冻结。
真正的分析未在验证中暂停或重启，5 个关键进程的启动时间一致，等待期限解除及自动证据
导出仍保留。16:30 UTC 核验余量约 5.76 GiB，仍在自动分析，完整查询结果尚未返回。
该失败、重试与资源保护状态另见[全量分析记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/full-analysis.json)。

本轮默认自动分析随后在 16:49:21 UTC 成功完成，首个 DNS 名称查询及证据导出在
16:49:27 UTC 完成。从启动到导出共约 2 小时 27 分钟；Ghidra 报告自动分析耗时
8,797 秒，其中“不返回函数识别”4,358.901 秒、反编译 switch 分析 2,986.670 秒，
两项合计约 83.5%。首次原生查询必须等待整套默认分析结束，这段时间主要用于建立程序
结构。主机有 2 个 CPU、约 2 GiB 内存，累计换页计数表明存在反复换页，但不能据此单独
算出换页增加了多少分钟。

成功响应中的 4 个名称匹配含外部符号和导入跳板，不能解释为 4 个不同的 DNS 实现；初始
证据包只记录了这次查询。分析日志仍有局部反编译和引用分析告警，完整函数恢复、解析调用
图和隐私路径覆盖都需要后续验证。当时已保存日志、各阶段耗时、响应和证据包，并保持
已分析会话打开。初始证据包与后续的完整数据库备份是不同产物，详见[完成与耗时记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/completed-analysis.json)。

随后通过 Ghidra 官方 `DomainObjectAdapterDB.saveToPackedFile` 接口保存了完整 Program，
文件为 400,201,462 字节，约 382 MiB，当时保存在 Buena Park 的研究目录。先用小样本验证，
再保存真实会话；没有重启 provider、强制终止事务或重新定义类。独立进程以 `-noanalysis`
重新打开后，62,462 个函数条目、10,838,796 条指令、428,428 个已定义数据项计数一致；
61,982 个内部函数的全部名称和地址范围摘要、内存块布局、完整 `.text` 摘要和 14 处
指令/注释/引用抽样也一致。该核对并非逐表穷举数据库所有逻辑字段；官方打包接口还可能
修剪 undo/redo 历史。导出工具、校验脚本、参数和摘要见[数据库恢复验证](https://github.com/shinokiri/claude-code-termux-musl/tree/main/research/native-2.1.296/checkpoint)。

恢复时须明确把 Java 临时目录放到持久文件系统，单独设置 `TMPDIR` 不足以改变该 JVM 的
`java.io.tmpdir`。本次首次恢复测试因此遇到 `/tmp` 配额，改为
`JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=<持久磁盘目录>` 后通过。使用 Ghidra 12.1.4 导入
`.gzf`、指定 `-noanalysis`，再用 `VerifyCheckpoint.java`、`live-config.json` 输出并比较
快照即可重复核对；不能只依据 headless 进程退出码判断成功。这里验证的是 Ghidra 数据库
恢复，上游 REA 尚未提供经验证的直接恢复入口。原会话当时继续用于后续查询。

**2026-10-12 收尾更新：** 用户授权清理后，服务器研究会话、工具和完整数据库已经删除。
仓库保留上述验证结果、摘要和脚本；这些记录不能代替 `.gzf` 文件，重新查询完整数据库
需要重新生成。历史记录中的保留路径不再存在，见
[服务器清理记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/server-cleanup-20261012/results.json)。

定向复核进一步利用了 38 次 REA 查询，其中 36 次成功；另 2 次因该地址没有被默认分析
识别为函数而拒绝反编译或函数信息查询。自动引用表只包含 5 个 `getaddrinfo` 直接调用点，
早期原始指令扫描和本次运行观察确认实际已知有 7 个。因此，工具报告“自动分析完成”并不
等于已经识别所有函数和引用。`0xef502d8` 也确认位于未初始化的 `EXTERNAL` 占位块，
通过 thunk 指向外部符号；它不是另一个解析器实现。

| 新增的独立进程测试 | `getaddrinfo` 调用点 | 验证内容 |
| --- | --- | --- |
| `Bun.dns.lookup` 的 `system`、`libc`、`getaddrinfo` 后端 | `0x1dea84c` | 三个入口均返回模拟解析器的回环地址 |
| `Bun.listen` 的主机名绑定 | `0x23b0f30` | TCP 监听成功，保留 `AI_PASSIVE`、流式 socket 提示 |
| `Bun.udpSocket` 的主机名绑定 | `0x23b1834` | UDP 绑定成功，本地接收器收到数据报 |
| `Bun.udpSocket` 的主机名连接 | `0x23b1cd8`，另含绑定点 `0x23b1834` | UDP 连接成功，本地接收器收到数据报 |
| `node:cluster` 共享句柄的主机名回退 | `0x19bd41c` | `_getServer` 配合 `sharedOnly` 触发；子进程收到并关闭共享 FD，退出后无残留 worker |

这 7 个新增测试均通过，补齐此前未触发的 4 处调用点；与早期 fetch/WSS 观察合并后，
七个**已知直接调用点**均有运行时证据，并进入现有兼容入口。cluster 测试刻意使用内部
`_getServer` 入口提供尚未解析的主机名，不代表普通 `net.Server.listen` 总会走这个回退。
新测试以当前 Bun 文档选择 API 参数，行为结论来自已核对安装二进制的实际运行和返回地址。
[DNS 参数](https://bun.com/docs/runtime/networking/dns)、[TCP 参数](https://bun.com/docs/runtime/networking/tcp)、[UDP 参数](https://bun.com/docs/runtime/networking/udp)。

复现时先运行上文的调用点实验，再复用它生成的独立观察库：

```sh
python3 research/native-2.1.296/dns-followup/study.py \
  --observer build/dns-callsite-study --output build/dns-followup-study
```

脚本校验原件、安装件和观察库，使用独立配置、本地模拟 DNS 与回环 UDP 接收器；每种 API
启动新进程，cluster 子进程及文件描述符在结束时回收。实验子进程显式使用 `standard`
以便隔离 DNS 研究，日常 `optional-off` 设置不被更改。没有模型请求或真实账号流量。
完整记录见[定向复核结果](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/dns-followup/results.json)。
“7/7”仅描述这七处直接调用指令，不代表所有 DNS 分支、间接回调、TLS 指纹或隐私路径
均已覆盖；本轮验证不构成对全部上报已关闭的承诺。

官方二进制仍由更新器直接跟随 npm 渠道，不要求为每个官方版本重新发布适配包。DNS 库与
其他运行文件共同校验、验收和原子切换。旧代理模式保留为显式诊断选项，不在原生失败时静默
回退。既有的隐私环境变量不由这层适配增删；本库也不包含域名拦截策略。

## 系统依赖与边界

最低要求 Android 10 / API 29；实测环境为 Android 16、ARM64、4 KiB 页面。直接使用的
`dnsproxyd` 命令是 AOSP 实现协议，不是稳定的 NDK ABI。ROM 权限变化、协议变化或上游
运行时改用不同解析入口，均可能需要后续适配。网络切换、长期待机、其他 Android 版本、
16 KiB 页面以及真实登录后的 Remote Control 尚未完成设备验证。

实现参考：

- [AOSP Bionic getaddrinfo](https://android.googlesource.com/platform/bionic/+/refs/heads/main/libc/dns/net/getaddrinfo.c)
- [AOSP NetdClient](https://android.googlesource.com/platform/system/netd/+/refs/heads/main/client/NetdClient.cpp)
- [AOSP DnsProxyListener](https://android.googlesource.com/platform/packages/modules/DnsResolver/+/refs/heads/main/DnsProxyListener.cpp)
- [Android NDK 原始 DNS 接口与 API 级别](https://developer.android.com/ndk/reference/group/networking)
- [Claude Code 进程包装协议和覆盖范围](https://code.claude.com/docs/en/corporate-launcher)
