# 发布适配更新

官方 Claude 版本由更新器直接跟随，不为每个官方版本发布一个适配版。本流程只在适配代码
发生变化时使用。`main` 上尚未发布的修改不会自动安装到用户设备。

1. 更新根目录 `adapter.json` 的数字版本号。已经发布的版本号及附件不可复用或覆盖。
2. 运行回归检查及设备验收，更新 README 和验证记录，再提交并推送。
3. 运行 `python3 tools/package-adapter.py --output /tmp/claude-adapter-release`。
4. 为该提交创建 `adapter-v<版本>` 正式 GitHub Release，上传产出的
   `claude-termux-adapter.tar.gz` 和 `update.json`，并将其设为 latest Release。

打包器只收录独立安装所需的显式文件清单，不包含 Claude 二进制、运行目录、Git 目录、
用户配置或凭据；相同源码和权限生成相同摘要。`update.json` 包含 schema、版本及归档
SHA-256。客户端从固定仓库的 `releases/latest/download/update.json` 读取清单，按版本
构造同一仓库的 Release 附件地址，只接受普通文件并拒绝路径穿越、链接及重复条目。

安装器协议版本为 schema 1。下载后的 `tools/install-user.py` 接受 `--data-dir`、
`--config-dir`、`--bin-dir`、`--version` 及内部 `--lock-fd`。`--lock-fd` 必须对应安装目录
的同一个锁文件，子安装器继承其打开文件描述；父进程等待完成期间不会释放锁。
退出码 0 后，父进程仍检查已激活的 Claude 版本、适配版本和文件摘要。

协议不兼容的未来更新应先提供过渡版本，不能仅改变清单 schema 后假设旧客户端能升级。
测试使用的 `0.0.0` 适配身份只允许出现在临时测试安装中，不作为正式 Release 发布。
