# IAM 自动恢复

## 适用范围

此模块实现本人授权的同济 Canvas 登录恢复。正常业务采集仍使用原来的 GET 路径/参数白名单；认证所需 POST 仅在独立 `auth/iam.py` 中允许，不开放任意请求或 HAR 重放。

认证顺序：

1. 验证 Bearer Token 和已配置的 Canvas Cookie。
2. 任一通道仍可用时沿用它，不提交 IAM 密码。
3. 两者明确失效或未配置，且启用 `IAM_AUTO_LOGIN` 时，获取数据库登录租约。
4. 默认创建全新 IAM 会话，不带旧 IAM Cookie；仅显式开启 `IAM_USE_SAVED_SESSION=true` 时尝试已有 SSO 会话。
5. 出现正常密码表单时，获取本轮参数和公钥，提交授权保存的账号密码。
6. 校验 OAuth state、Canvas 本人 API 和已绑定账号，然后原子替换 Canvas / IAM Cookie 文件。

网络故障、429、5xx、403 不能当作凭据过期而反复登录。未知页面结构、未知目标域名、密码拒绝、验证码或增强认证会中止自动流程，不猜测下一步。

每轮同步先校验认证；发现 Token 和 Cookie 都失效后，在同轮完成 IAM 恢复并继续采集。默认轮询间隔为 300 秒，另加上一轮采集耗时。身份接口的 Cookie 404 会通过课程接口交叉校验；只有后者明确拒绝认证才触发恢复。成功后清除失败冷却，新的会话失效不会被上一次成功登录的冷却阻挡。失败重试仍保留退避和登录互斥。一次免增强认证成功不代表学校今后不会要求增强认证。

## 来自 HAR 的协议证据

原始 `canvaslogin.har` 位于用户提供的本地目录，不复制到可发布源码。只复用协议结构，动态认证值每次重新获取。

```text
Canvas /login/openid_connect
  → IAM /idp/oauth2/authorize
  → IAM /idp/AuthnEngine
  → IAM /idp/authcenter/ActionAuthChain（登录页）
  → POST /idp/displayVerificationCode.do（判断是否需要验证码）
  → GET /idp/themes/default/js/main/crypt.js（本轮公钥）
  → POST /idp/authcenter/ActionAuthChain（RSA 加密密码）
  → POST /idp/AuthnEngine（结束本轮认证）
  → IAM /idp/profile/OAUTH2/AuthorizationCode/SSO
  → Canvas /login/oauth2/callback
  → GET /api/v1/users/self（身份验收）
```

两个需要保留的实际协议细节：

- IAM 可能使用显式 `:443`，它与省略端口的 HTTPS 源等价，其他端口仍拒绝。
- 验证码接口返回 false 时，正常网页仍提交登录页的验证码占位文本。模块保留当前页面字段值；若接口要求验证码或登录结果要求增强认证，则停止，绝不猜解或绕过。

密码使用当前 IAM 公钥进行 RSA PKCS#1 v1.5 加密，并遵循抓包观察到的序列化方式。动态 `authnLcKey`、认证链、OAuth state、授权码、Cookie、密码不进入日志。日志只保留方法、主机、路径、状态码和错误分类。

## 本地配置

```dotenv
IAM_AUTO_LOGIN=true
IAM_USERNAME_FILE=secrets/iam-username
IAM_PASSWORD_FILE=secrets/iam-password
IAM_COOKIE_FILE=secrets/iam-cookies.json
IAM_TIMEOUT_SECONDS=90
IAM_RETRY_SECONDS=900
CANVAS_COOKIE_FALLBACK=true
# 保持此前已经验证过的资源白名单，必须包含 identity
```

账号密码只放在上述私密文件中，文件权限 600；配置示例不填真实值。首次连接和增强认证由本人在学校网页中完成：

```sh
uv run canvas-notifier auth login --remember-iam
```

此命令正常登录后保存 Canvas 与 IAM 各自主机的 Cookie，不保存浏览器中的 IAM 密码，也不导出整个浏览器配置。程序可以读取显式提供的 `IAM_PASSWORD_FILE`，这与浏览器抓取密码是不同的操作。

验证后台恢复：

```sh
uv run canvas-notifier auth iam-test
# 明确要求发起新的 IAM 登录交换，即使当前 Canvas Cookie 有效：
uv run canvas-notifier auth iam-test --force
```

`--force` 只跳过本服务的重试冷却，不跳过学校验证码、增强认证或账号一致性检查。

## 状态和重试

健康页中的 `iam` 显示：`attempting`、`session_saved`（交互会话已保存）、`ok`、`retry_wait`、`interaction_required` 或 `blocked`。

- 临时网络/服务错误：默认 15 分钟后重试，指数退避，最长 6 小时。
- 密码拒绝或增强认证：停止重复提交。更新授权凭据文件、完成 `auth login --remember-iam` 或明确执行测试命令后才重新尝试。
- 跨进程数据库租约防止同时登录；登录开始前保存冷却状态，进程中断不会立刻造成重试风暴。
- 新 Cookie 只有通过普通 Canvas API 的再次校验后才替换旧文件；账号不一致或验证失败保留旧凭据与数据库。
- 恢复成功和需要处理时产生状态变化通知，重复的相同阻塞不会每轮发送邮件。

## 验收与边界

使用独立临时数据库验证：Token 不可用且 Canvas Cookie 为空时，后台可以使用经本人增强认证后保存的 IAM SSO 会话重新取得 Canvas Cookie，再校验绑定身份与课程读取。真实账号、数量和运行时间不写入公开仓库。

无有效 IAM 会话时会尝试密码流程；学校若要求短信增强认证、验证码或其他验证，自动流程停止并通知本人，不承诺永久免交互。服务器出口和设备环境变化需部署时重新验证。

自动登录成功仅更新健康状态和凭据文件，不发送 IAM 成功邮件，也不因同轮恢复发送认证降级或同步恢复邮件。IAM 登录失败仍告警；课程内容变化和真实采集失败的通知不受影响。
