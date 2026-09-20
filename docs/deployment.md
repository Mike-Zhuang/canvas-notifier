# 独立服务器部署

仓库只包含源码、合成/去敏样本与模板。真实配置、凭据、数据库及验证记录必须单独通过安全通道传输。

## 目录与进程

- `/opt/canvas-notifier/releases/<revision>`：只读版本目录；`current` 指向活动版本。
- `/etc/canvas-notifier/service.env`：真实配置，限制访问。
- `/etc/canvas-notifier/secrets/`：独立的 Canvas、IAM、SMTP 和管理登录凭据，目录 700、文件 600。
- `/var/lib/canvas-notifier/state.db`：SQLite 数据库，WAL 模式，FULL 同步和 30 秒 busy timeout。
- `/var/lib/canvas-notifier/backups/`：私密备份，不纳入 Git。

`canvas-notifier-web.service` 与 `canvas-notifier-worker.service` 由专用非 root 用户运行，启用自动重启、只读系统路径和进程内存上限。Web 仅绑定 `127.0.0.1:18400`，外部通过 Nginx HTTPS 访问。

项目也支持 PostgreSQL。在内存较小且旧系统无法直接安装受支持 PostgreSQL 的单用户服务器上，可以使用已通过同套测试的 SQLite。不要为迁移使用旧版本数据库包或影响其他站点。

## 安装顺序

1. 审阅源码、Git 索引和构建包，执行测试与敏感信息扫描。
2. 安装 Python 3.12+，在新 release 中执行 `uv sync --locked --no-dev`。
3. 创建专用用户、配置和私密目录；根据 `deploy/service.env.example` 生成真实配置。
4. 停止原 worker，迁移一致性快照及已发送记录；数据库与凭据不经过 GitHub。
5. 以服务用户执行 `alembic upgrade head`、`doctor` 和 SMTP 测试。
6. 安装 `deploy/` 中的 systemd units；配置 Nginx 域名和证书路径，先 `nginx -t` 再 reload。
7. 启动 Web/worker，检查 HTTPS、登录/CSRF、逐资源同步和真实邮件入箱；只保留一个正式 worker。
8. 启用并验证 `canvas-notifier-backup.timer`，每天 UTC 03:30 备份，并验证 SQLite 完整性。

Certbot 使用独立 ACME webroot；证书续期成功后的 deploy hook 应先检查 Nginx 配置再 reload。

## 验证与回滚

```sh
systemctl status canvas-notifier-web canvas-notifier-worker
journalctl -u canvas-notifier-worker --since '10 minutes ago'
curl -fsS https://YOUR_DOMAIN/healthz
systemctl start canvas-notifier-backup.service
systemctl list-timers canvas-notifier-backup.timer
```

需要回滚时先停止 worker，保留当前数据库备份，再将 `current` 指向上一版本。仅在兼容的数据库迁移上回退；不要直接覆盖运行中的 SQLite 文件。恢复时 SQLite WAL/SHM 不能当成独立业务数据库搬运，应使用原生 backup API 生成一致性副本。

学校可能限制服务器出口或要求 IAM 增强认证。部署成功不能代替该环境的实际身份验证；没有可用会话时应在健康页准确显示认证中断，不能把它当作没有更新。
