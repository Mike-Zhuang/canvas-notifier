# 本地运行与故障处理

## 进程与状态

`web` 只提供有认证的管理界面；`worker` 的采集循环与每 15 秒的提醒/投递循环独立运行。SMTP 失败不会终止采集。当前课程约每 5 分钟采集，文件等内容约 15 分钟；已结束但仍在观察期的课程约每小时。范围内每次执行都完整分页，因此无需靠不可靠的 updated_at 或 graded_since 增量游标发现成绩和评论。

近触发任务最多每分钟复核一次作业与本人提交。数据库租约防止多个 worker 并行重复采集或投递；进程退出后租约会过期。`/healthz` 检测 Web/数据库活性，详细同步状态、资源完整性、认证降级和 worker 心跳仅在登录后的健康页显示。没有配置服务器外部心跳服务，本地进程全部停止时无法自发告警。

`sync_scopes` 保存每个范围的页数、数量、最近尝试/成功时间及错误分类。到达分页安全预算后保留 cursor 并明确标记 partial，下一次从首尾完整扫描重试，不将部分结果视作全量。清单不再出现的资源标为待确认缺失，保留原快照；不会把 401、403、404 或部分分页解释为老师删除。

## 认证恢复

1. `uv run canvas-notifier auth token-test` 验证 Bearer；`--watch` 将每次时间、状态码和状态写入被忽略的 `.local/auth-watch.jsonl`。
2. Token 到期后人工重新生成并替换 `secrets/canvas-token`。没有 refresh token，不会自动生成或修改学校令牌。
3. `uv run canvas-notifier auth login` 打开浏览器，由本人完成 IAM 登录；成功后验证账号一致性并保存 Canvas 主机 Cookie。先测试单独的 `_canvas_middle_session` 是否足够，若不够保留该主机通过验证的必要 Cookie 集。
4. `uv run canvas-notifier auth token-test --mode cookie` 验证 Cookie。备用通道仅对经过读取验证的种类启用，例如 `CANVAS_COOKIE_RESOURCES=identity,course,assignment,submission,announcement,file,folder,discussion,reply,module,module_item,page,conversation,calendar,planner,planner_note`。没有测过的种类不要添加。
5. 需要双通道失效后的自动 IAM 恢复时，配置 `IAM_AUTO_LOGIN=true`，并按 [IAM 自动恢复](iam-authentication.md) 设置凭据和增强认证会话。
6. 设 `CANVAS_COOKIE_FALLBACK=true`，仍保留 `CANVAS_AUTH_MODE=token`。每轮先检查 Bearer；明确 401/登录 HTML 后只对已验证资源切换。403 表示资源权限问题，不切换去绕过限制。两个凭据必须对应同一 Canvas 用户。

配置和凭据在新一轮采集创建客户端时读取。SMTP/Web 的环境配置在进程启动时读取，改 `.env` 后需由操作者重启对应本地进程。数据库规则保存后下一次 tick 生效。学校可使 Cookie 失效，持久保存不保证长期有效。

## 邮件状态与恢复

默认所有新内容和变化均即时发送，不区分文件、回复或其他资源。发送循环每 15 秒检查一次 outbox；这表示发现后尽快投递，不是等待每日固定时刻。发现延迟仍由拉取频率决定，网络/SMTP 失败会影响最终送达。

运行 `canvas-notifier notifications-immediate` 可以将已有全局、课程及任务覆盖规则统一为即时，关闭静默并释放排队中的摘要。命令不会重发已接受邮件或重置事件去重状态。

outbox 状态：pending 计划发送，sending 持有发送租约，accepted SMTP 接受，retry 等待退避，failed 已达到 8 次失败，cancelled 计划不再适用。失败退避从一分钟开始，上限一小时。在通知历史可手动重试失败邮件。通知事件被关闭或未配置收件人的原因保存在 events；提醒被取消或抑制的原因保存在 reminder_jobs。

邮件重试前复核任务是否已完成、边界是否已经过去、规则是否仍适用。临近截止复核失败时遵守 `stale_source_policy`。静默策略允许穿透、提前或抑制关键提醒，不能一律延迟到截止之后。错过多个触发点会合并为仍有意义的一封提醒。

SMTP 服务器可能改写 Message-ID。验证入箱应结合主题、发送时间和匹配的 Message-ID 尾部；不要因为原 ID 的精确搜索无结果就认定未收到。项目没有提供邮件回复回写、自动标已读或退信 Webhook。普通 SMTP 接受与最终送达须分别报告。

## 备份与恢复

```sh
uv run python scripts/backup.py --verify
```

PostgreSQL 使用 `pg_dump -Fc` 备份到 `.local/backups/`，恢复到随机隔离数据库，比较 resources、events、reminder_jobs、outbox、notification_rules 行数，随后移除验证库。验证时宜停止 worker 避免源数据变化。SQLite 使用原生 backup API 并检查完整性。备份权限 600，不包含 secrets 文件，但仍含学习数据，应私密保存。

正式恢复前停止本地 worker，备份现有库，人工选择备份文件并恢复到新数据库；确认完整性后更换 `DATABASE_URL`。不要直接覆盖正在写入的数据库。恢复后提醒按已持久化的状态补偿，已接受邮件不会因普通重启自动重发。

## 安全与发布

- HTTP 客户端只允许既定 GET 路径和参数；不重放 HAR、任意 GraphQL、签到、提交、改配置或标记已读。
- 邮件链接只指向已验证的 Canvas HTTPS 主机和业务路径；去除查询凭据。HTML 去掉脚本、图片及危险标签，邮件头拒绝换行。
- 管理站点使用独立密码和签名会话 Cookie（HttpOnly、SameSite=Strict；HTTPS 时 Secure），表单 CSRF 校验、来源检查和登录频率限制。Canvas 凭据不进入 HTML、前端存储或 API 响应。
- `secrets/` 权限 700，凭据文件权限 600。Typer 异常不展示局部变量。错误日志只记录安全分类，不记录凭据或原始响应正文。
- `.gitignore` 对原始计划、HAR、`.env`、凭据、数据、日志与备份单独忽略；运行 `scripts/check-public-files.py` 检查候选发布文件。不要把本地截图、运行报告或数据库附到公开 issue。


## 持续故障告警与重试

从同一轮各资源结果汇总一次故障观察，取消逐资源失败/成功邮件。默认至少连续失败 3 轮且持续 300 秒才发送一封故障邮件；同一故障持续期间不因错误类型变化重复告警。仅当故障邮件已被 SMTP 接受、且连续 2 轮恢复正常后发送一封恢复邮件。尚未投递的旧故障邮件在稳定恢复时取消。窗口保存在数据库 Health 中，重启不会重置。可配置 `SYNC_ALERT_FAILURES`、`SYNC_ALERT_SECONDS`、`SYNC_RECOVERY_SUCCESSES`。

该窗口监测此前成功读取过的资源中断以及全局认证/同步失败。从未建立成功基线的资源（例如长期不可用的 Pages 接口）保留健康页异常，不以此持续发信。失败资源所属课程会优先重新读取，不等内容资源的 15 分钟周期。

单请求网络错误、429、5xx 最多尝试 4 次，等待约 2、4、8 秒并增加随机抖动，遵守服务端 Retry-After。一轮仍失败后等待 60、120、300 秒，此后保持 300 秒重试（另加执行耗时）。IAM 临时登录错误仍独立按 15 分钟起指数退避、上限 6 小时；密码拒绝/增强认证停止自动提交，等待本人处理，故障邮件统一经过上述窗口。正常认证失效在下一轮同步开始时自动恢复。

## 同轮文件合并

同一课程、同一同步轮次、同一收件人的文件和文件夹即时事件合并成一封资料更新邮件，等待该轮同步结束后立即投递；不发送额外的逐文件邮件。不跨课程合并，显式配置每日摘要的规则仍遵循原规则。每一项完整保留标题、原始文件名、文件类型、大小、文件夹 ID、源时间、发现/同步时间、变更明细和链接。发送批次上限不会拆散同一组；失败时整组按现有 SMTP 60 秒起指数退避、上限 1 小时、最多 8 次的策略重试，保持同一 Message-ID。
