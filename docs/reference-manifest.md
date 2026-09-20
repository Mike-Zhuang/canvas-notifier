# 固定参考版本与复用方式

实施先读取参考客户端，再扩展资源状态、调度、outbox 和管理站点。下列版本于 2026-09-19 固定；本地 checkout 在被忽略的 `.local/references/`，不作为运行依赖。

| 项目 | 固定 commit | 采用方式 |
|---|---|---|
| [brilliant751/tongji-canvas-mcp](https://github.com/brilliant751/tongji-canvas-mcp/tree/2f35afd8915e2e4036d3308ba7915fda7133c022) | `2f35afd8915e2e4036d3308ba7915fda7133c022` | 主要 REST 行为来源：Bearer、GET、Link 分页、课程/作业路径、include 参数、身份探测和生命周期 watcher。未复制 FastMCP 外壳，也未复制原始代码。 |
| [xksnetcbs/tongji_canvas_calendar](https://github.com/xksnetcbs/tongji_canvas_calendar/tree/75629e1e607f91db2025dd939276cadda10f6281) | `75629e1e607f91db2025dd939276cadda10f6281` | 登录后保存会话、跨轮询保存状态的行为参考。未采用 due_at-only 过滤、CalDAV 或修改截止来表示完成。 |
| [yzxoi/tongji-canvas-iOS](https://github.com/yzxoi/tongji-canvas-iOS/tree/87cc3be1a0a1886dfdc31163b5dd6021bfc4e2a1) | `87cc3be1a0a1886dfdc31163b5dd6021bfc4e2a1` | 仅参考 Cookie 捕获与保存语义；没有实现 iOS、移动 OAuth、签到或 Keychain 客户端。 |
| [instructure/canvas-lms](https://github.com/instructure/canvas-lms/tree/1c9f0bb8013ed69c4f2efe11fd483025469b7e6c) | `1c9f0bb8013ed69c4f2efe11fd483025469b7e6c` | 固定上游 HEAD，用于协议语义定位；应用运行不依赖其源码。邮件信息结构和 API 语义参考，未复制 Rails/AGPL 实现。 |

MCP 和第三方 iOS 参考仓库当时未发现根目录许可证；因此移植接口设计与行为，代码独立实现。calendar 带 MIT 许可，本实现没有直接复制其代码。公开分支不代表学校正在运行相同版本。

## 模块映射

- `canvas/http.py`：继承 MCP 的基础行为，改为长生命周期 AsyncClient、字符串 ID、无展示数量截断的分页；新增有限重试、Retry-After、同源 next Link、路径/参数白名单、304 缓存、Cookie 分通道和健康状态。
- `canvas/adapters.py`：继承课程与 assignment 请求结构；扩展本人 submission 详情、公告和其他资源，保留完整分页及嵌套模块/讨论读取。
- `cli.py`：身份探测和 watcher 参考 MCP 的验证流程；不会输出原 watcher 中的响应正文、用户名或 Token。
- 交互登录：采用 calendar 的“正常浏览器登录 → 持久化会话 → 轻量 HTTP 读取”流程；不逆向 IAM 或重放登录 POST。
- `domain/`、`sync/`、`scheduling/`、`delivery/`、`web/`：新增实现，解决参考项目尚未覆盖的状态变化、lock-only、成绩可见性、持久提醒、可靠邮件和配置页面。

## 协议依据

[Assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments)、[Submissions](https://developerdocs.instructure.com/services/canvas/resources/submissions)、[Conversations](https://developerdocs.instructure.com/services/canvas/resources/conversations)、[分页](https://canvas.instructure.com/doc/api/file.pagination.html)、[限流](https://canvas.instructure.com/doc/api/file.throttling.html)。同济实际响应及本地 HAR 的优先级高于当前上游描述。

作业 API 文档明确 `override_assignment_dates` 默认为 true；日期调度以该学生身份请求得到的 due_at/lock_at/unlock_at 为准。submission 的 cached_due_date 仅在作业日期字段缺失时兜底，不用缓存覆盖明确 null 或老师改后的新时间。

## IAM 恢复新增参考（2026-09-20）

- [Jinitaimei_Server](https://github.com/Mike-Zhuang/Jinitaimei_Server/tree/dd0a3537d8331eb10961b6ec9401cf02036f6aab)：固定 `dd0a3537d8331eb10961b6ec9401cf02036f6aab`，参考 `app/clients/tongji.py` 的 IAM RSA、表单序列化和验证分层行为。
- [Jinitaimei](https://github.com/Mike-Zhuang/Jinitaimei/tree/f8dccce336a5e919ee773c7dfecc536b233eec11)：固定 `f8dccce336a5e919ee773c7dfecc536b233eec11`，参考已有会话优先、密码兜底和增强认证交互的设计。
- 用户提供的 `canvaslogin.har`：认证跳转及 Canvas callback 的直接证据；原始文件不发布。

参考项目采用 AGPL。此处仅参考协议行为，基于 HAR 和本轮响应独立实现受限的 Canvas IAM 适配器，没有复制项目源代码。未采用一系统专用的 session/login 接口，也未扩大业务 API 的只读白名单。
