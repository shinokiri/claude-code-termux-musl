# 原生 Android DNS 与执行边界

适配器 1.1.0 的默认结构是官方 musl 二进制、一个进程内 DNS 兼容库和小型执行包装。
它替换 1.0 的默认 HTTP CONNECT 代理，不改变 Claude Code 内部 JavaScript，也不安装
JavaScript API 补丁。musl 加载器与 ELF 解释器路径适配仍然必要。

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

## 为什么执行包装仍然保留

同一设备、同一程序的对照测试结果：

| 路径 | 结果 |
| --- | --- |
| musl Claude 直接启动 Bash，执行 `#!/usr/bin/env python3` 脚本 | 退出 126，`/usr/bin/env` 解释器不存在 |
| 经 `termux-shell` 恢复 Bionic termux-exec 后执行同一脚本 | 退出 0，脚本成功 |
| 直接自启动原始 Claude 二进制 | 新进程没有加载 DNS 兼容库 |
| 经官方 process wrapper 自启动 | 新进程正确加载 DNS 兼容库，保持 PID 和完整参数 |

DNS 库必须在加载后移除 `LD_PRELOAD` 环境变量，否则 Bionic 子程序会尝试加载 musl 库。
这也意味着 Claude 的新进程需要在入口重新加载它。Shell 包装负责相反方向的环境恢复。

可以继续编写原生 `exec` / `posix_spawn` 拦截来承担这些职责，但那需要处理不同调用入口、
显式环境、脚本解释器、文件动作和信号语义。当前没有证据证明它比使用官方的两种包装接口
更可靠或更容易跟随更新。现有包装以 `exec` 交接，不留下额外的常驻监督进程。

官方包装有其范围：Chrome 原生消息宿主、深链首个进程和 `--worktree --tmux` 的首个进程
等并不全部经过它；命令 Hook 测试也不代表所有 exec-form Hook 均已覆盖。本次结论是补齐
已识别的 DNS 调用路径，不是承诺拦截所有未来的系统调用或为所有桌面集成功能提供支持。

## 验证与更新

安装/升级候选版必须通过以下本机检查后才能替换工作版本：

- 12 类记录（A、AAAA、ANY、CAA、CNAME、MX、NAPTR、NS、PTR、SOA、SRV、TXT），每类
  5 种 API 入口；命名 ESM 导入、TTL、反向解析、Bun DNS、4 种 Worker 情况。
- NXDOMAIN、无记录、SERVFAIL、REFUSED、超时、取消、64 个并发查询、分段 IPC 返回、
  48 条 TXT 的大响应以及 24 个并发大响应；显式服务器保持原始路径。
- HTTPS fetch、Node HTTPS、WSS、原始 Host/SNI，以及错误主机名证书被拒绝。
- 真实 CLI 的前台、包装器自启动和后台 daemon 工具链，包括 Bash、文件工具、Hook 和 MCP。
- 真实 musl 子进程在父进程退出后仍可完成 HTTP 请求和 TXT 查询。

上述测试用本地模拟 DNS/API；另在本机真实 Android DNS 服务上验证了地址与 TXT 查询。
实际验收报告保存在安装版本的 `acceptance.json`、`dns.json`、`network.json` 和各工具报告中。
不同 Bun 版本的 TXT 分块和无记录错误可能有差异；测试使用同一二进制的显式 UDP DNS 作为
对照，确认兼容层保留该二进制本来的行为，不把 Node 文档中的行为硬写成补丁。

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
