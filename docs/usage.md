# Codex Session Backup 使用指南

先按 [README](../README.zh-CN.md#快速开始) 安装依赖并完成合成数据演练。下文命令从项目目录运行。

## 配置 COS 直连

工具通过 restic 原生 S3 后端连接 COS，仓库地址为 `s3:https://<endpoint>/<bucket>/<prefix>`。

1. 在腾讯云创建一个私有的标准 COS 桶，选一个专用前缀。检查生命周期规则，避免备份对象自动过期或转入需要解冻的存储类型。
2. 从模板创建本地配置。配置存放在 macOS 的用户数据目录，密钥和仓库口令存放在当前用户的钥匙串。

   ```sh
   backup_data="$HOME/Library/Application Support/Codex Session Backup"
   mkdir -p -m 700 "$backup_data"
   cp -n connection.example.json "$backup_data/connection.json"
   ```

3. 编辑 `connection.json`。填写完整桶名（包含 APPID）、仓库前缀、地域码和当前机器可达的 COS 服务端点。端点只填写主机名，例如广州公网 `cos.ap-guangzhou.myqcloud.com` 或内网 `cos-internal.ap-guangzhou.tencentcos.cn`。设定固定的 `backup_host`，让同一备份源的快照归入同一保留组。填写自己的 GPG 主公钥完整指纹，以及可下载该公钥的 HTTPS 地址。 [腾讯云 COS 地域和访问域名](https://cloud.tencent.com/document/product/436/6224)
4. 导入仅能访问备份前缀的写入凭据。准备执行保留清理时，再导入独立的维护凭据。命令通过终端隐藏输入。

   ```sh
   ./codex-backup credentials
   ./codex-backup credentials --maintenance
   ```

### 写入与维护权限

写入身份可列出备份前缀、读写该前缀内的对象并删除 `locks/` 中的锁。维护身份可额外删除该前缀中的对象。两种身份都应被拒绝列出桶根目录和访问其它前缀。

用 `python3 provision_cos.py` 预览两份 CAM 策略。可以在 CAM 中自行创建身份并导入凭据，或安装 `tccli` 后使用桶所属账号的 profile 创建两份身份并写入钥匙串。执行前确认 profile 和预览策略。工具使用固定的 CAM 用户与策略名称，遇到不同用途的同名资源时会停止。

```sh
python3 provision_cos.py
python3 provision_cos.py --profile PROFILE --apply
```

### 从旧接入迁移

已有 COS 直连配置和钥匙串凭据可以沿用。若原先使用中转连接，在单独的数据目录复制一份配置，删除 `transport` 字段，选择当前机器可达的 COS 服务端点并导入专用凭据。下文每条命令都可在子命令前加 `--data-dir /absolute/path/to/data`。数据目录分离配置、运行状态和搜索缓存。钥匙串条目按 macOS 用户共享，切换已有直连凭据前先确认当前任务使用的身份。

复用同一仓库时，保持桶、前缀、`backup_host` 和 restic 口令一致。先运行 `probe`，再从已有快照做空目录恢复。迁移到新仓库时，将原仓库视为独立的历史备份并按新仓库流程初始化。

## 生成恢复包和首份备份

将恢复包写到已存在目录下的新文件。首次运行会生成随机 restic 口令。工具核验配置中的完整主公钥指纹，使用 GnuPG 加密恢复包。恢复包和可解密的 GPG 私钥都需要有独立于 Mac 和 COS 的恢复路径。

```sh
./codex-backup setup-key /path/to/recovery.gpg
./codex-backup verify-key /path/to/recovery.gpg
```

换机恢复时，先配置原仓库、恢复可解密的 GPG 密钥并导入 COS 凭据，再运行 `./codex-backup recover-key /path/to/recovery.gpg`。已有恢复包的用户可直接使用该包。

已有 restic 仓库运行 `./codex-backup probe`，它会解锁仓库并以只读方式列出快照。空前缀首次运行 `./codex-backup init` 创建仓库。

```sh
./codex-backup run
./codex-backup status
./codex-backup check --full
```

`run` 回读远端清单、对象清单和样本会话，通过后记录已验证的快照 ID。`check --full` 读取完整仓库数据，耗时和流量随仓库增长。

## 搜索、恢复与回滚

```sh
./codex-backup browse
./codex-backup browse --snapshot SNAPSHOT
./codex-backup restore --snapshot SNAPSHOT --to /path/to/empty-restore
```

搜索页只监听本机回环地址，可按关键词、项目、更新日期和归档状态筛选。终端打印的搜索地址带有访问令牌，请仅在本机使用。按 Ctrl-C 关闭搜索页。历史搜索会解密对应快照的索引。更新日期按 UTC+8 筛选。

会话详情默认在本地渲染 Markdown。关闭 Markdown 开关可查看原文，也可以导出 Markdown。右侧目录支持跳转到消息和 Markdown 标题。正文中的图片显示为替代文本，快照中的图片可以从附件下载。

左侧「附件」列出消息中引用且在所选快照中的文件，以及快照中按会话 ID 存放的产物。点击下载按钮时，工具从同一快照读取、解密并校验该文件，无需完整恢复。查看附件清单和下载需要能访问备份库。窄屏下，附件和目录移到正文上方，可以折叠。

`restore` 根据快照清单校验所有恢复文件。目标目录必须为空。需要放回 Codex 使用时，恢复阶段加 `--codex-root /absolute/path/to/final/.codex`，使恢复副本内的会话日志指针适配最终位置。

1. 结束活跃任务并退出 Codex。在目标数据目录旁建回滚目录，移走本次恢复涉及的同名文件、同名目录及对应的 SQLite WAL/SHM 文件。保留源数据和恢复目录。
2. 把恢复结果的 `codex/` 内容放到最终 Codex 数据目录。使用兼容的 Codex 版本和原账号启动，检查普通、归档和压缩会话。
3. 如需回滚，退出 Codex，移走本次放入的文件和目录，再从回滚目录还原原文件。恢复结果中的 `catalog.sqlite` 和清单可留在恢复目录供查验。

## 定时备份与保留

手动备份并验证空目录恢复后，从稳定的安装位置执行 `./codex-backup install-agent`。LaunchAgent 在用户登录期间每小时检查一次，距离上次成功约 24 小时后尝试备份。它也会执行每周仓库检查、约每 90 天全量检查和每月保留预览。Mac 登录状态、钥匙串解锁和网络连通性会影响运行。用 `status` 查看最近的成功快照、`scheduled_successes` 和错误。定时运行日志保存在数据目录的 `launchd.log`。

数据目录的 `retention-preview.txt` 保存每月预览。当前保留策略为最近 2 份，以及 30 天日备份、12 周周备份和 12 个月月备份。保留组由 restic 按主机名、路径和标签计算。确认预览和最新恢复点后，才使用维护身份执行删除。

```sh
./codex-backup maintain
./codex-backup maintain --apply
```

停止定时备份时，卸载并移除 LaunchAgent 定义。之后仍可运行手动命令。

```sh
launchctl bootout "gui/$(id -u)/com.waynegong.codex-session-backup"
rm "$HOME/Library/LaunchAgents/com.waynegong.codex-session-backup.plist"
```

## 备份范围和兼容性

- 默认读取 `~/.codex`。覆盖 `sessions`、`archived_sessions`、对应的会话和任务状态 SQLite、会话索引，以及 Codex 数据目录下的附件、生成图片和其他受支持产物。备份和恢复针对工具支持的本地 SQLite 与日志结构，升级 Codex 后可先做一次隔离恢复验证。
- `auth.json`、全局配置、Git 工作区和外部文件走各自的备份或登录流程。会话正文和附件可能包含用户粘贴的敏感内容。restic 加密远端快照，本地缓存、恢复结果及 Markdown 导出包含可读内容，适合存放于启用 FileVault 的卷。
- 默认状态和缓存目录是 `~/Library/Application Support/Codex Session Backup`。使用 `--local-repo` 演练时应同时指定隔离的 `--data-dir`、合成数据的 `--source`，并设置测试专用的 `CODEX_BACKUP_TEST_PASSWORD`。本地演练模式与日常 COS 备份的凭据管理相互独立。
