# 1.2：进程内执行兼容层

默认运行路径由两份使用同一份 `termux-exec.c` 的原生库承担：musl 库负责 Claude 内部的
执行与 DNS，Bionic 库负责 Termux 工具再次执行程序时的 ABI 切换。日常模式不设置
`CLAUDE_CODE_SHELL_PREFIX` 或 `CLAUDE_CODE_PROCESS_WRAPPER`，不再构建 `termux-shell`
或 `claude-dispatch` 可执行文件。

```text
claude 命令入口（配置、自动更新，然后 exec）
  → 官方 musl Claude + ELF 自动加载的 musl 兼容库
      → Claude / daemon / session：各自通过 ELF 加载同一版本兼容库
      → Bash / Python / Node / MCP：加载 Bionic 执行库和 termux-exec
          → 再次执行 Claude：清除 Bionic loader 变量，由 ELF 加载 musl 兼容库
```

## 加载不再依赖父进程传递 preload

安装器先校验官方 npm 包，修改私有 musl 解释器路径，然后添加指向本版本
`libtermux-musl-compat.so` 的绝对 `DT_NEEDED` 依赖。直接自启动、显式精简环境以及多代
子进程都由 ELF 加载器建立兼容层，不需要 JavaScript preload 或包装器来重新注入。

这里不能直接使用普通 `patchelf --add-needed`：在实测的 2.1.296 ET_EXEC 上，即使添加
一个空库也会导致启动崩溃。`tools/patch-runtime.py` 在解释器补丁生成的独立 header LOAD
空隙中复制动态字符串表和动态表，更新加载元数据；原有程序段的文件偏移、虚拟地址和内容
保持原样。它验证 ELF 类型、架构、表范围、段间空隙及现有 section，拒绝没有安全空间的
布局。不会通过搜索压缩代码中的函数名或改写 Claude 的 JavaScript 来适配。

此方法有明确的上游格式依赖。未来官方包更换 ELF 布局、运行时或调用入口时，候选版可能被
拒绝，需要更新适配代码。被拒绝的版本不会替换当前工作版本；这不是保证所有未来版本都能
立即安装。运行文件含绝对路径，必须在最终版本目录构建，不支持直接搬动目录。

## 执行边界

执行库读取目标 ELF 的 `PT_INTERP`，区分 Android linker 与其他动态 ELF。进入 Bionic
时恢复它的执行库和已保存的 Termux loader 环境；进入 musl 时移除不兼容的 Bionic loader
变量。已有 termux-exec 仍在 Bionic 侧负责它原有的系统 linker 等平台行为。

脚本经过执行权限检查和 shebang 解析，支持单个可选解释器参数以及 `env -S`；常见
`/bin/`、`/usr/bin/`、`/sbin/`、`/usr/sbin/` 路径映射到 Termux 的 bin 目录。PATH 搜索、
ELF 的 `argv[0]`、空参数和 Unicode 参数保留。`execve`、`execv`、`execvp`、`execvpe`、
`execl`、`execlp`、`execle`、`fexecve` 都有入口。

当前 Linux ARM64 Bun 的已测 spawn、spawnSync 和 execFile 路径最终调用 `execve`。
musl 执行拦截路径使用栈上的临时数据，不分配堆内存、不查找符号、不修改全局环境，也不获取
DNS 库的锁。检查 ELF 的临时文件直接调用关闭系统调用，避免 vfork 子进程误入 DNS 的
描述符管理。原生 `exec` 保留 PID，兼容层不会增加常驻子进程或转发第二次终端信号。

这些入口不是任意程序的通用执行沙箱。直接发出 exec 系统调用、绕过可替换符号的
`posix_spawn`/`execveat`、静态链接工具以及自行移除执行库的程序，不属于当前拦截保证。
普通外部工具仍可通过公开的 `claude` 命令入口启动，不能把裸 ELF 路径视为所有启动方式下
都等价的公共入口。重新构造脚本参数或环境时最多接受 16,384 个条目，超过返回 `E2BIG`；
两个重写的 loader 值各限制在 131,000 字节；脚本解释器嵌套最多 5 层。没有声称复刻所有 libc、内核和桌面集成的边界行为。

## 版本清理与运行进程

1.4 的 musl/Bionic 库持有 `runtime/release.lease` 的共享文件锁，跨 exec 保留，
在 spawn 清理描述符后于受支持的执行入口重新取得。后台和工具进程独立保护其所用版本，
重复执行不持续增加租约描述符。清理必须取得独占锁；若清理已开始，新进程在主程序运行前
明确失败。1.7.5 起当前版始终保留，上一版按实际占用决定是否删除，删除时同步移除
`previous` 指针。Python 管理命令也持有共享租约，直到最后一次读取安装文件完成；
安装锁和管理器租约释放后，再由当前版执行一次不等待忙锁的清理。
旧管理器迁移中的收尾文件会在安装事务内临时保护。没有租约的历史版本仍需人工检查。
现有会话监视器在有旧版时继续触发本地清理，并在前台退出后再检查一次；子进程仍持锁则保留。
这不是对主动绕过兼容层或主动关闭全部保护描述符的程序的保证。

## 迁移、回退和备用模式

1.2 安装时只删除本安装曾写入的确切 `CLAUDE_CODE_PROCESS_WRAPPER` 值，保留自定义
包装器、其他配置及隐私开关。回退到 1.1 时恢复旧版所需值；存在其他自定义包装器时不覆盖。
旧会话仍可通过稳定的 `claude-process-wrapper` 入口调用旧版本运行文件，新入口保留这个
兼容分支。1.2 默认启动不使用它。

保留 `libtermux-musl-dns.so` 到新库的发现用别名，使 schema 1 的旧管理器能识别新版本的
原生 DNS。若从旧管理器回切新版，启动器在不阻塞更新锁的前提下完成同样的已知配置迁移。
这个别名不增加第二份库或第二条默认加载路径。

显式 `CLAUDE_TERMUX_DNS_MODE=http-proxy` 是旧传输诊断模式，使用进程包装入口为子进程
重建各自的备用代理。它与默认原生执行/DNS 模式分开，不会因某次解析失败而自动启用。

更新候选必须通过 53 个进程用例、DNS、TLS、前台工具、后台 daemon/session，以及父进程
退出后的子进程 HTTP/TXT 检查。完整目录被校验后才切换 `current`，不对活动版本就地修改。
源码调试默认使用仓库 `build/runtime/`；构建器会拒绝改动已经封存的正式安装目录。

## 已验证与仍待验证

2.1.296 / Bun 1.4.3 / Android 16 的实测覆盖：5 种 Bun/Node 进程 API × 9 种目标场景，
加上 Bionic 工具中的 8 个 libc exec 入口；另验证 2,000 个参数、stdin、额外 fd、cwd、
SIGTERM、ENOENT/EACCES、ugrep/bfs 的 argv[0]，临时自定义 preload/库路径经过
Bionic → musl → Bionic 后仍生效、显式空环境不继承这些自定义值，以及真实 CLI 的 Bash、Read、Write、Edit、
Grep、Glob、命令 Hook、stdio MCP、daemon 和后台会话。三代自启动每代使用显式精简环境。

这些结果来自真实程序和受控本地 API，不等同于真实账号、付费模型、Remote Control 或长期
待机的验收。执行兼容层不屏蔽遥测，不伪造系统身份、TLS 指纹、设备可信状态或风控信号。
它不能提供网络隐私隔离保证。默认隐私设置由用户配置决定，与本次兼容重构独立。
