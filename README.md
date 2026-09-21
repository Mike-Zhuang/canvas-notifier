# Canvas 个人通知服务

面向本人 Canvas 账号的只读通知服务。后台持续采集课程、作业、本人提交与成绩、公告、文件、讨论、站内信、模块、页面和日历计划，比较快照并通过 SMTP 发送邮件。管理站点提供总览、提醒规则、通知历史和连接健康。

支持本地运行及独立服务器部署；官方 iOS App 对接不在本项目范围内。服务器部署见 [部署说明](docs/deployment.md)。

## 本地启动

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync --locked
cp .env.example .env
chmod 600 .env
uv run canvas-notifier init
uv run alembic upgrade head
uv run canvas-notifier web
```

访问 <http://127.0.0.1:8000>。管理密码自动生成在 `secrets/admin-password`，不要把它提交到 Git。另一个终端运行：

```sh
uv run canvas-notifier worker
```

Web 与 worker 是不同进程。仅打开网页不会开始采集和发送。电脑休眠或进程退出后不会继续轮询；重新启动会读取持久化提醒和 outbox。

默认示例使用 SQLite，适合体验和离线测试。也支持 PostgreSQL，并使用相同测试套件验证：

```sh
# 需要本机提供 initdb / pg_ctl / createdb / psql
./scripts/local-postgres.sh
# .env 中设置：
# DATABASE_URL=postgresql+asyncpg://canvas_notifier@127.0.0.1:55432/canvas_notifier
uv run alembic upgrade head
```

专用 PostgreSQL 仅绑定 `127.0.0.1:55432`，使用本地 trust 认证以便开发；这不是生产部署配置。不要将端口开放到其他机器。

## Canvas 认证

优先使用个人 Bearer Token，保存到 `secrets/canvas-token`，文件权限必须为 600。不要在命令行参数中携带 Token。

```sh
chmod 600 secrets/canvas-token
uv run canvas-notifier auth token-test
uv run canvas-notifier auth token-test --watch --interval 300 --max-checks 24
```

Token 失效时不会虚构 refresh token。Cookie 认证需要本人正常登录：

```sh
uv sync --locked --extra browser
uv run playwright install chromium
uv run canvas-notifier auth login
uv run canvas-notifier auth token-test --mode cookie
```

登录完成后自动验证本人身份，仅保存 Canvas 主机 Cookie，文件位于 `secrets/canvas-cookies.json`。如果浏览器账号与数据库账号不同，会拒绝覆盖。

启用备用通道前需验证 Cookie 对同一资源的读取能力。在 `.env` 中配置 `CANVAS_COOKIE_FALLBACK=true` 和 `CANVAS_COOKIE_RESOURCES`；仅名单中的资源允许从失效 Bearer 切换，403 不会触发切换。详见 [运行维护](docs/operations.md)。

## IAM 自动恢复

可启用第三层恢复：Token 与 Canvas Cookie 都明确失效后，通过已授权 IAM 会话或保存的账号密码重新取得 Canvas Cookie。支持登录互斥、冷却、身份校验与原子保存；遇到验证码或增强认证会停止并报告。

配置、命令和实测边界见 [IAM 自动恢复](docs/iam-authentication.md)。需要保存正常交互认证后的 IAM 会话时运行 `uv run canvas-notifier auth login --remember-iam`。账号密码放在 `secrets/iam-username`、`secrets/iam-password`，不要写进 `.env.example` 或源码。

## 邮件与规则

**默认所有内容变化都在发现后立即入队发送，包括文件、讨论回复、公告、作业与成绩。** 不再对任何内容类别默认安排每日摘要；首次同步仍建立历史基线，重复采集不会重发已通知的事件。摘要与静默仅作为用户主动选择的可选配置保留。

已有实例可执行 `uv run canvas-notifier notifications-immediate`：统一全局、课程、任务范围的通知方式，关闭静默延迟并释放尚未发送的内容摘要；已接受邮件不重发，SMTP 失败重试仍保留必要退避。

在 `.env` 中填写自己的 SMTP 主机、TLS 模式、发件地址、收件地址。SMTP 密码保存在 `secrets/smtp-password`，权限 600。`MAIL_TEST_TO` 是独立测试收件人配置。

```sh
uv run canvas-notifier mail test
uv run canvas-notifier sync --once
uv run canvas-notifier reminders preview
uv run canvas-notifier doctor
```

管理站点支持全局、课程和单任务规则；单任务优先。正式截止 `due_at`、停止提交 `lock_at`、开放时间和个人计划分别建模。`due_at=null` 的任务不会漏掉 `lock_at` 提醒。

规则偏移采用正 ISO 时长，如 `P3D, PT24H, PT1H`；空列表表示关闭该边界提醒。不支持不定长的月份。支持静默时段、每日摘要、逾期次数限制、仅未完成提醒、数据过旧时暂停、个人完成标记与暂缓。高级 JSON 支持部分字段继承、各类事件开关以及 `recipients` 覆盖。保存前可以预览实际触发时刻。

首次成功采集按资源范围建立历史基线，不会逐条推送历史成绩。普通 SMTP 为可重试投递，在“已接受但尚未记账时进程崩溃”的边界可能重复；不承诺 exactly-once。SMTP 已接受也不等于收件箱已送达。

## 离线体验和开发收件器

```sh
DATABASE_URL=sqlite+aiosqlite:///.local/demo.db uv run canvas-notifier demo
DATABASE_URL=sqlite+aiosqlite:///.local/demo.db uv run canvas-notifier web --port 8001
# 独立终端：本地 SMTP 收件器，不外发邮件
uv run python scripts/dev-inbox.py
```

若使用本地收件器，将 SMTP 设为 `127.0.0.1:1025`、`SMTP_TLS_MODE=none`，用户名和密码留空。收件器将邮件保存在 `.local/mail/*.eml`。请勿让演示数据与真实账号共用数据库。

`sync --once --dry-run` 读取 Canvas，但仅写临时数据库，不修改正式快照、游标、已发状态，也不发送邮件。它与无需联网的合成演示是不同用途。

## 验证与备份

```sh
uv run pytest -q
TEST_DATABASE_URL=postgresql+asyncpg://canvas_notifier@127.0.0.1:55432/canvas_notifier uv run pytest -q
uv run ruff check src tests scripts migrations
uv run alembic check
uv run python scripts/check-public-files.py
uv run python scripts/backup.py --verify
```

PostgreSQL 测试使用随机独立 schema；备份恢复验证创建随机本地测试数据库并在完成后移除，不覆盖正式库。备份包含个人学习数据，保存在被忽略的 `.local/backups/`。

更多说明：[能力矩阵](docs/capability-matrix.md)、[实现与验证记录](docs/verification.md)、[参考来源及许可证](docs/reference-manifest.md)、[运行维护](docs/operations.md)。

## 上传 GitHub 前必须检查

`.gitignore` 已明确忽略：

- **`har/` 和所有 `*.har` 原始抓包**；
- **`Tongji_Canvas_Enhancement_Plan*.md` 等原始实施计划**；
- **`secrets/`、`.env` 和其他真实环境配置**；
- `.local/`、数据库、邮件文件、日志、备份、浏览器会话及参考仓库 checkout。

可发布的 `.env.example` 只有配置名称和示例值，`tests/fixtures/har-sanitized.json` 只包含白名单抽取、替换身份与内容后的回归样本。请运行 `scripts/check-public-files.py` 再提交。`.gitignore` **不会自动移除已经被跟踪或历史提交中的敏感内容**；该项目初始化时原始输入并未加入索引。不要使用 `git add -f` 绕过忽略规则。
