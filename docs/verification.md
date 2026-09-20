# 验证与发布审阅

## 可重复运行的验证

```sh
uv run pytest -q
TEST_DATABASE_URL=postgresql+asyncpg://USER@127.0.0.1:PORT/DATABASE uv run pytest -q
uv run ruff check src tests scripts migrations
uv run alembic check
uv run python scripts/check-public-files.py
uv build
```

测试覆盖完整分页、只读边界、认证异常、历史基线、作业与评分变化、零分、日期变更、规则继承、静默时段、持久提醒、错过触发点合并、邮件重试、并发租约、登录 CSRF、IAM/OIDC 恢复，以及 HTML/纯文本邮件信息一致性。

PostgreSQL 测试在随机独立 schema 中运行，完成后清理；普通离线测试使用临时 SQLite。IAM 测试使用合成学校服务和运行时生成的 RSA 密钥，不访问真实账号。单独的公开 RSA 公钥只用于测试 Base64 包含 `//` 时的解析，不含任何私钥。

## 样本隐私

公开 fixture 只保留回归所需的结构和状态关系：外部 ID、标题、分数、文件大小与日期均已替换；时间映射至固定合成时间轴。原始 HAR、原始文件哈希、用户身份、课程正文、Cookie、Token、账号密码、真实邮箱以及本地运行截图不随仓库发布。

原始数据提取工具的输出仍需审阅，不可因为文件名含 sanitized 就跳过检查。`scripts/check-public-files.py` 会对 Git 候选文件扫描已配置的真实凭据，并检查私密路径忽略规则。

## 运行环境验收

部署时应分别验证身份、课程读取、规则与任务恢复、SMTP 接受和实际入箱、HTTPS 会话属性以及数据库备份恢复。SMTP 接受并不等于最终送达。实际账号的完整验证记录只留在本地私密目录，不写入公开文档。

没有实际非空样本的资源不能声明为真实非空验证通过；查看已登录健康页面的逐资源验证等级。网页接口 403/404 不视为删除或空列表，也不通过换身份绕过。
