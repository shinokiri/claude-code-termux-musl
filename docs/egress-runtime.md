# 进程树与系统调用出口核查

对象固定为 Claude Code 2.1.296、ARM64 musl、适配器 1.7.2、`optional-off`
策略修订 6。研究直接在这部 Termux 设备上运行。此轮没有发布适配器新版，也没有
修改日常安装的隐私策略。

## 结果与范围

在测试保护器启用后，五个真实 CLI 场景完成了 **383,817 次系统调用、360 个
线程/进程 ID、67 条有发送数据的 TCP 连接、67 个解密后的 HTTP 请求**的核对。
其中包含 25 次合成模型请求。线程 ID 的数量不是独立进程数量。

| 场景 | 跟踪 TID | TCP 连接 / HTTP 请求 | 合成模型请求 | 原有事件上报批次 |
| --- | ---: | ---: | ---: | ---: |
| 前台 standard | 42 | 12 / 12 | 3 | 2 |
| 前台 optional-off | 33 | 9 / 9 | 3 | 0 |
| optional-off 流式失败恢复 | 33 | 11 / 11 | 5 | 0 |
| 后台 standard | 120 | 21 / 21 | 7 | 6 |
| 后台 optional-off | 132 | 14 / 14 | 7 | 0 |

这不是仅用域名或请求数作比较。分析器根据内核记录的 socket inode 汇集成功写入
的字节，分离 CONNECT 头，再逐连接校验 TLS 字节的长度与 SHA-256；本地代理与
TLS 接收器之间的连接地址将其关联到解密后的请求。**1,863,971 个客户端 TLS
字节全部与接收记录对应**。TLS 握手及关闭阶段的数据不冒充 HTTP 载荷；关闭时
晚到的字节单独计数。

两种后台模式都运行了原程序的 daemon、会话及备用进程，并在完成合成任务后结束
本次测试创建的进程。后台还会请求会话辅助信息，所以模型请求数量多于前台。
本次不把前台的三个请求断言套用到所有后台模型调用。

汇总、逐连接摘要、原始跟踪文件的校验和见
[trace-results.json](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/trace-results.json)。
保护器关闭前的发现与修正必须同时阅读，不能只引用这张通过表。

## 实际发现：外部 SSH 不受主程序代理约束

早期后台探查发现，官方插件仓库准备流程会启动系统 `ssh`，执行 GitHub SSH
认证探测；还出现了 SSH 形式的 Git 仓库访问尝试。这些子进程绕过了夹具 HTTP
代理和主程序的 DNS 桥。`HOME` 指向临时目录并没有阻止 OpenSSH 根据系统用户
目录打开日常 `.ssh` 下的默认密钥及 known_hosts。

因此，早期后台发现过程**不是“完全本地、没有访问日常凭据”的测试**。发现后
停止了未受保护的运行，保留并检查原始记录，才继续后面的验收。

对两轮后台发现过程共六次 SSH 执行的复核结果为：四次发送了 SSH 协议标识、
`KEXINIT` 和密钥交换方法初始化报文，另两次没有成功的 Internet socket 写入。
完整的已发送字节均能按该阶段的明文报文格式解析；未观察到发送 `NEWKEYS`
或后续用户认证数据。由此不能声称发生了私钥上传，也不能否认默认凭据路径被
访问。协议编号依据 [OpenSSH ssh2.h](https://github.com/openssh/openssh-portable/blob/master/ssh2.h)。

原程序模块 `chunk-a7h7w6pe.js` 中的 `bgt` 函数确实运行该探测，并根据返回码和
认证成功消息判断 SSH 是否配置好；相关逻辑参与 Git 传输方式选择。这项证据
支持“功能性认证探测”的解释，不能仅凭发生网络发送就定性为遥测。
版本固定的模块、代码范围摘要以及逐次复核见
[helper-discovery.json](https://github.com/shinokiri/claude-code-termux-musl/blob/main/research/egress-2.1.296/helper-discovery.json)。
复核脚本只读取跟踪记录，不读取密钥文件，也不输出其内容。

这一发现说明：主程序内的隐私控制、DNS 兼容层和 HTTP 代理都不能自动成为
外部工具的完整通信边界。日常运行仍需将插件自动准备、用户主动 Git 操作和
可选报告分别判断；本次没有给它们统一套上阻断策略。

## 观察器验证与测试保护

先用独立 C 程序验证跟踪器确实能够看到：

- 直接执行 AArch64 `svc #0` 的 `write`，程序计数器与反汇编位置相符。
- `writev`、`sendfile`、UDP `sendto` 和 `sendmmsg`。
- fork 子进程经 `SCM_RIGHTS` 收到 socket 后发送。
- 子进程 `setsid`、复制描述符、exec 后继续向继承的 socket 写入。

接收端得到预期的 82 个 TCP 字节和 22 个 UDP 字节。这个正对照避免把 libc
拦截器能够观察的范围误当作系统调用范围。真实 CLI 也执行了主 ELF 内的直接
写入指令；本轮观察到的相关写入面向文件和管道，不是额外的网络流。

研究保护器 `fixture-guard.c` 使用 seccomp 用户通知检查连接与发送地址，保留
127.0.0.1 / ::1 和本地 IPC，拒绝外部 IP、系统 DNS 代理、原始网络 socket、
新建 io_uring 等未建模机制，并在本研究中拒绝启动真实 `ssh`，避免再次触及默认
身份文件。独立负对照验证了直接 `connect`、UDP 三种发送接口和 SSH exec 被拒绝。
最终两个后台场景分别记录到一次被拒绝的 SSH 启动。

过滤器会随被测进程的 fork/exec 继承；通知接收器通过 `pidfd_getfd` 获取自己的
seccomp listener，分析器对这次已知描述符复制单独校验。相关机制见
[Linux seccomp 文档](https://docs.kernel.org/userspace-api/seccomp_filter.html)。

**这是研究用的意外直连保护，不是日常隐私模式，也不是对恶意代码的完整沙箱。**
它读取指针参数后使用 `CONTINUE`，有并发修改参数的时间差；允许的本地 IPC、
文件、设备及系统服务也可能形成进一步委托。独立 strace 记录仍是核查依据。
被拒绝的 SSH 成功分支不计入功能验收。

本轮 IPC 可追溯到已记录的 socketpair、后台监听/连接/接收关系，或 Android
`/dev/socket/fwmarkd`。后者会接收网络 socket 描述符，属于进程树之外的系统
服务；本研究没有追踪该服务内部的后续行为。

## 后台保留的功能请求

在本地 TLS 接收器观察到了这些路径：

- 更新日志：`raw.githubusercontent.com/.../CHANGELOG.md`。
- 模型目录和签名：`downloads.claude.ai/model-catalog/v1/...`。
- 官方插件目录、压缩包及 GitHub 仓库准备。
- 模型、配置、权限限制、功能分配和 bootstrap 等业务请求。

夹具为下载/仓库请求提供的是合成响应，未验证真实模型目录签名、完整插件安装
或 GitHub 登录。通用合成 JSON 还使插件版本字符串形成了一次无效的压缩包 URL；
该路径保留在记录中，但不能当作真实服务下的正常下载路径。

两种后台模式都保留上述功能尝试。事件上报的正对照出现，`optional-off` 的已知
上报未到达接收器；这并不意味着所有功能请求中的所有字段已完成隐私分类。

## 复现

需要固定版本的安装、Python、Clang/LLVM 和支持本机跟踪的 strace。本次使用
Termux strace 7.2；仅将 strace、libdw、libelf、argp 的包解压到研究目录，
没有改变系统安装。其额外库目录只供跟踪器使用，不传给被测 CLI。

```bash
python3 research/egress-2.1.296/trace-study.py \
  --strace /path/to/extracted/strace \
  --tracer-libraries /path/to/extracted/lib \
  --output build/trace-check
python3 research/egress-2.1.296/trace-analyze.py \
  --input build/trace-check \
  --output build/trace-check-analysis.json
```

`--challenge-only` 只运行观察器正对照，`--background-only` 运行后台两种模式。
真实 CLI 场景默认使用研究保护器；每次必须用新输出目录。原始记录可能包含
合成会话的完整环境及数据，不能把这种采集方式直接用于日常会话。

剩余范围包括真实账号与 Remote Control、真实插件/SSH 功能成功分支、其它 MCP
或原生辅助进程、未触发的动态代码，以及进程树外的 IPC 接收方。ptrace 和保护器
会改变时序，也能被程序检测到；本轮证据不排除观察条件影响行为。
