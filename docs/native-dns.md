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
它来自独立的 Ghidra 有界分析，不代表另一项全量 REA 原生分析已经完成。
其定位、摘要和分析边界见[原生入口研究记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/uv-stubs.json)。
DNS 适配的现有覆盖结论仍以调用路径测试为依据；实际内部调用链还需要进一步追踪。

同日的全量 REA 查询在桥启动前因 `Disk quota exceeded` 失败；工作目录当时位于约
981 MiB、启用用户配额的 `/tmp` tmpfs。错误响应与证据包已保存，原临时目录由 REA 的
失败处理自动回收，失败前留存的快照及独立局部项目仍保留。随后仅将新会话的 `TMPDIR`
移到 ext4 数据目录，保持目标、工具源码和分析配置相同；截至 14:23 UTC 已进入自动分析，
完整查询结果尚未返回。该失败与重试状态另见[全量分析记录](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/native-2.1.296/full-analysis.json)。

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
