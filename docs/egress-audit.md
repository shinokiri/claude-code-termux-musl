# 固定版本的数据出口审计

对象是 Claude Code **2.1.296 / Bun 1.4.3 / ARM64 musl**，安装适配器为 **1.7.2**，
`optional-off` 策略修订为 **6**。本次增加研究工具、静态清单和组件运行证据，
不修改安装中的程序或隐私策略。

机器可读清单及每项待查范围见
[coverage.json](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/coverage.json)。
清单区分程序内容、静态指令位置、实际执行场景和数据用途；一个层面的完整计数
不自动代表另一个层面也已经覆盖。

## 已取得的证据

| 范围 | 当前结果 | 结论边界 |
| --- | --- | --- |
| 程序内嵌文件 | 先前逐项校验了 2,589 个 VFS 文件；对 2,345 个文本 JS 模块建立语法索引 | 索引中的 3,459 个发送名称候选包括普通对象方法；动态或改名调用可能不在其中 |
| 压缩资源 | 先前单独解码 138 个资源 | 解码不代表其代码已纳入上述 JS 调用索引；该索引排除的 3 个压缩 JS 资源继续单列 |
| 主 ELF 可执行区域 | 扫描全部 56,087,136 个文件内可执行字节 | 不包含 JIT 输出、其他共享库、子进程或额外加载代码 |
| ELF 导入跳板 | 解码全部 476 个 PLT 跳板，并逐个与 GOT / JUMP_SLOT 重定位对应 | 指向这些跳板的直接分支可以枚举；间接函数指针调用需要另查 |
| 选定底层接口 | 2,459 处直接分支，涵盖网络、文件描述符、执行和加载接口 | `write` 等名称不能说明描述符实际指向文件、管道还是网络 |
| 通用系统调用 | 535 处内联 `SVC`，948 处直接调用 `syscall@plt` | 对 1,480 处取得有界直线路径的调用号依据，其余 3 处通过额外路径核对补齐；不是全程序可达性证明 |
| gateway 转发组件 | 两种模式各 14 个场景通过，本地接收器共收到 38 次预期请求 | 复用原始代码片段，发送帮助函数、日志与 Google 凭据提供者由夹具替代；未启动完整认证服务 |
| 进程树运行核查 | 五个受保护的真实 CLI 场景，67 条 TCP 发送流与 67 个 HTTP 请求逐字节摘要对应 | 早期后台运行发现绕过代理的外部 SSH；保护后拒绝该研究分支，不能混称为全功能或无凭据访问验证 |

原生扫描直接读取 ELF 字节、动态符号与重定位，不依赖 Ghidra 是否识别了某个函数。
它同时发现 PLT 外的 7,126 个 `BR` 和 44,221 个 `BLR` 指令模式；这些位置可能是
运行时分派、普通函数调用等，不能一概归为网络调用。本次没有逐个恢复它们的目标。

这些是对齐指令模式的扫描结果。可执行区域中的数据也可能与指令编码一致，
因此仍需保留“候选”及可达性限制。七处 `getaddrinfo` 直接调用与先前实际运行验证的
集合相符；原生 DNS 证据见[DNS 研究](native-dns.md)。

## 系统调用号核对

有界分析使用 LLVM 解码每个位置之前的指令，只接受明确的寄存器立即数赋值，
以及不会修改该寄存器或转移控制流的已检查后缀。系统调用名称取自本机 AArch64
`asm/unistd.h` 的展开宏。无法满足条件的位置保留为未解析，随后单独复核：

- `0x2e21d00` 和 `0x2e21e60`：共同的 `x8=79` 定义，经测试位分支到达两个
  `newfstatat` 调用。复核了分支目标和两条路径上的寄存器保持关系。
- `0x18bb9a4`：`x0=277` 的定义在较长的过滤器构造代码之前，超出自动窗口；
  扩展范围后确认到 `syscall@plt` 的直线路径保持该值，对应 `seccomp`。

REA 的现有 Ghidra 会话查询 `0x2e21e60` 时仍返回 `not_in_procedure`。
这项结果与原始指令证据一起记录，说明自动函数表不能作为指令清单的唯一来源。
额外核对脚本及结果见
[review-syscall-gaps.py](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/review-syscall-gaps.py) 和
[syscall-gap-results.json](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/syscall-gap-results.json)。

在已经取得调用号依据的这些路径中，内联 `SVC` 主要执行文件操作，另有退出、
进程句柄及事件等待操作。其中 **107 处直接执行 `write`**。通用 `syscall()`
路径还包含 `writev`、`sendfile` 等描述符操作。

这些发现要求后续观察实际文件描述符的来源和去向。它们本身不证明发生了上报，
也不能用“没有发现独立的 socket 系统调用号”推导没有网络外发：已有网络描述符
可以经通用写入接口发送数据，间接调用和其他加载代码也不在这个有限集合中。

调用号结论以给定指令路径为前提。任意其他入口、间接跳转和运行时改写都没有
通过这项静态研究排除。

## gateway 组件的范围

已定位的启动入口为 `claude gateway --config <path>`。配置结构中
`telemetry.forward_to` 默认为空列表；配置某个接收目标时，默认启用 metrics，
logs 和 traces 默认关闭。该功能用于服务操作者显式配置的 OTLP 转发，不能仅凭
程序包中存在这一实现，推断普通交互式 CLI 会向这些目标发送数据。

本次从真实安装包读取并校验整个模块摘要，再按固定 AST 边界提取六段原始代码：
转发类、路径与阈值常量、日志地址清理函数、路由片段及客户端设置生成函数。
代码片段重新求值时，显式提供夹具依赖；没有改写原始片段内部逻辑。
各片段摘要和位置保存在
[gateway-profile.json](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/gateway-profile.json)。

运行验证确认：

- 未配置接收目标时，路由返回 200，不读取传入请求体，不产生转发请求。
- 根据 metrics / logs / traces 开关选择目标，按原样传递请求体，并保留被检查的
  内容类型、内容编码及操作者配置的头。被转发请求不是这个组件新收集的本机环境资料。
- 达到 128 个并发转发时丢弃新增任务；连续五次目标失败后开启暂停转发的断路器。
  被测的 400 载荷拒绝不计入该断路器，多个目标的失败状态相互独立。
- 配置转发目标和 `listen.public_url` 后，设置生成函数会输出启用客户端 OTel
  的环境设置；缺少任一条件时返回空结果。这里只验证设置生成，没有验证真实下发或客户端应用。
- `optional-off` 已初始化的实验进程和 `standard` 进程均完成预期组件转发。
  这项对照仅说明被重放的组件请求能够通过当前传输控制，不代表完整服务全部行为已经验收。

所有发送都由夹具限制到本地接收器，使用合成载荷和合成凭据。原始 `Is` 发送帮助函数
包含的地址检查、DNS、代理及 TLS 逻辑在这里被替代，未计入验证。
Postgres、OIDC、会话认证、完整服务启动、真实外部接收器和托管设置下发仍待验证。
本次据此保留现有策略，没有把服务操作者配置的转发归为普通客户端可选上报并停用。

## 复现与下一项验收

在对应版本的 Termux 安装上运行；输出必须使用新的目录或文件。需要 Python、
Clang/LLVM 以及当前安装的 Claude，无需重新下载完整程序或重跑 REA 全量分析。

```bash
python3 research/egress-2.1.296/native-scan.py \
  --output build/egress-check/native.json
python3 research/egress-2.1.296/syscall-context.py \
  --scan build/egress-check/native.json \
  --output build/egress-check/syscalls.json \
  --evidence build/egress-check/windows.json
python3 research/egress-2.1.296/review-syscall-gaps.py \
  --scan build/egress-check/native.json \
  --context build/egress-check/syscalls.json \
  --output build/egress-check/gaps.json
python3 research/egress-2.1.296/gateway-study.py \
  --output build/egress-check/gateway
```

进程树验收已推进到直接系统调用、描述符转交、真实前台/后台运行及 TLS 字节关联。
过程发现了外部 SSH 认证探测，并修正了“临时 HOME 加 HTTP 代理即可隔离”的假设。
具体通过条件、早期非本地发送、凭据路径访问及研究保护器限制见
[运行核查](egress-runtime.md)。网络中已加密的字节不能直接用于判断隐私字段。

下一项应沿新识别的外部辅助程序边界继续：区分后台自动准备与用户显式 Git/插件
操作，选择不会把正常功能误当遥测的控制位置，并核对 IPC 接收方与尚未触发的路径。

真实账号功能、Remote Control、任意第三方工具和插件、动态代码及服务端数据处理仍有
独立的待查范围。本清单提供逐项推进的依据，不构成“所有隐私收集已经移除”的保证。
