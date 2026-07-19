# cloudflare_temp_email 与有头浏览器复用设计

## 背景与结论

当前工作副本对应上游 `lingxiaoyiyu-hub/grok-register-win` 的 `4167ed4`。对照以下实时版本完成审查：

- `huslx/grokzhuce@4697d44`
- `dreamhunter2333/cloudflare_temp_email@99b3323`

现有项目并非完全缺少 Cloudflare 临时邮箱入口：配置示例、Web 面板和 `CFWorkerMailbox` 已出现相关字段。然而它还不等价于 `huslx/grokzhuce` 的实现：

1. `cloudflare` 在 `mail_providers.make_mailbox()` 中被折叠到通用 `cfworker` 工厂。
2. 面板保存的创建、收信和 Token 路径没有被该工厂实际消费。
3. 收信固定调用管理员接口 `/admin/mails`，没有使用创建地址后返回的地址 JWT 调用 `/api/parsed_mails`。
4. 地址 JWT、`address_id` 和地址删除生命周期没有完整封装。
5. `x-custom-auth` 只存在于通用 CF Worker 配置，Cloudflare 专用表单没有完整表达参考项目的四项配置。

有头 Chromium 也确实没有被批次复用：Web 面板为每个账号启动一个 CLI 子进程，CLI 每轮结束又无条件执行 `restart_browser()`，随后外层 `finally` 马上关闭新浏览器。历史日志 `data/logs/run_20260716_223145.log` 共 476 轮，出现 104 次“浏览器连接失败”；日志可直接观察到“注册成功 -> 再启动浏览器 -> 连接失败/重试 -> 任务结束”。

## 目标

1. 把 `cloudflare_temp_email` 建成独立、可测试的邮箱提供商，协议与参考项目一致。
2. 在 Web 面板提供清晰、完整且向后兼容的配置页面。
3. 批量注册时复用同一个有头 Chromium 进程，同时隔离账号状态。
4. 浏览器失联、代理变化或硬超时时才重建进程，并且只清理本项目拥有的进程。
5. 保留单账号硬超时能力、停止能力、CPA 转换和现有其他邮箱源。
6. 通过自动化测试、真实 UI 检查和 Windows 进程计数证明行为。
7. 保留原项目历史与 MIT 归属，发布到公开仓库 `aiis2/grok-register-win`。

## 不做的事项

- 不部署或托管 `cloudflare_temp_email` 服务本身。
- 不改变 Grok 注册步骤、CPA 数据格式或其他邮箱供应商协议。
- 不结束用户自己打开的 Chrome/Edge。
- 不把 `config.json`、账号文件、邮箱凭据、日志、虚拟环境或参考仓库发布出去。

## 邮箱架构

### 独立提供商

新增 `CloudflareTempEmailMailbox`，保留 `CFWorkerMailbox` 供其他自建兼容服务使用。规范提供商 id 为 `cloudflare_temp_email`；旧 id `cloudflare` 作为兼容别名迁移到新提供商，而不再落到 `cfworker`。

核心配置：

- `cloudflare_api_base`：Worker 根地址。
- `cloudflare_admin_password`：`x-admin-auth` 管理密码。
- `cloudflare_domain`：创建邮箱时使用的域名。
- `cloudflare_site_password`：可选 `x-custom-auth` 站点密码。

兼容读取旧字段：

- `cloudflare_api_key` -> `cloudflare_admin_password`
- `defaultDomains` -> `cloudflare_domain`
- `cfworker_custom_auth` -> `cloudflare_site_password`（仅在专用字段为空时）
- `cloudflare` -> `cloudflare_temp_email`

保存新表单时写入规范字段，并同步必要的旧字段，确保旧版本主程序仍能读取；不再向用户暴露没有被协议使用的 Token 路径或任意鉴权模式。

### 请求数据流

创建地址：

1. `POST {base}/admin/new_address`
2. Header：`x-admin-auth`、可选 `x-custom-auth`、`Content-Type: application/json`
3. Body：`name`、`domain`、`enablePrefix: false`
4. 保存响应中的 `address`、地址 `jwt` 和可选 `address_id`

收取验证码：

1. 首选 `GET {base}/api/parsed_mails?limit=10&offset=0`
2. Header：`Authorization: Bearer <address_jwt>` 与可选 `x-custom-auth`
3. 从 `subject`、`text`、`html` 提取 Grok 验证码
4. 兼容旧服务时，可回退到 `GET /api/mails` 并解析 raw MIME；不得回退到需要长期管理员权限的轮询模式
5. 保留 `before_ids`、`otp_sent_at` 和取消检查语义

清理地址：

1. 首选使用地址 JWT 调用 `DELETE /api/delete_address`
2. 如果服务禁用了用户删除且已取得 `address_id`，回退 `DELETE /admin/delete_address/{id}`
3. 清理失败只记录告警，不覆盖已经完成的注册结果

### 错误处理

- URL、管理员密码和域名缺失时在保存配置和任务启动前给出明确错误。
- 401/403 区分 Admin 密码与站点密码问题。
- 404 `/api/parsed_mails` 才触发 raw API 兼容回退；其他服务端错误直接报告。
- 429 使用有上限的退避，不无限增加轮询压力。
- 日志不输出完整 JWT、管理员密码或站点密码。

## Web 面板设计

邮箱下拉中使用“Cloudflare Temp Email（推荐自建）”，选择后显示：

- Worker API 根地址
- Admin 密码
- 邮箱域名
- 站点密码（可选）
- “测试连接”按钮及状态区域

配置 API 的 GET 返回字段供表单回填；POST 做规范化和必填校验。新增测试连接 API，只验证配置能访问公开设置/创建协议所需的服务能力，不创建永久邮箱；如果服务没有安全的探测接口，则只验证根地址可达和必要配置格式，并在 UI 中明确说明。

密码字段保持当前本地面板的回填行为，但所有配置响应仍受现有登录保护。面板默认只监听 `127.0.0.1`。

## 有头浏览器生命周期

### 根因

1. `panel.job_worker()` 对每个账号调用 `_run_one_round()`，后者每次创建全新 CLI 子进程。
2. 子进程的 `run_registration_cli(count=1)` 在轮次 `finally` 中总是 `restart_browser()`。
3. 完成循环后外层 `finally` 立即 `stop_browser()`，形成无意义的第二次启动。
4. `ChromiumOptions.auto_port()` 每次分配新端口和 `autoPortData` 资料目录，天然不能附着旧实例。
5. `browser.quit(del_data=True)` 对 `Chromium` 使用默认 `force=False`；失败被吞掉后马上启动新实例，没有等待或确认旧 PID 退出。
6. 面板的窗口标题清理规则无法可靠识别 Chrome 子进程，也不能安全地区分用户浏览器。

### 批次复用模型

Web 面板按“批次”启动 CLI，而不是按账号启动 CLI。CLI 接收剩余账号数，并输出机器可解析的轮次标记：

- `ROUND_START index=<n>`
- `ROUND_RESULT index=<n> status=<success|failed>`

父进程根据 `ROUND_START` 重置单账号硬超时。某一轮超时时，父进程只终止当前批次进程树，记录失败，再用剩余数量启动新批次；因此正常账号共享浏览器，而卡死时仍能恢复。

### 账号间重置

成功或普通失败后调用 `prepare_browser_for_next_account()`：

1. 验证浏览器 CDP 连接仍然存活。
2. 关闭多余标签页，只保留或新建一个空白页。
3. 清除 Cookie、缓存、local/session storage 和 service worker 状态。
4. 导航到 `about:blank`，确认页面可响应。
5. 保持原浏览器进程、调试端口、代理和资料目录不变。

重置失败时才调用 `restart_browser()`。邮箱重试若页面状态已污染，可先执行账号间重置；只有失联时重启。

### 精确进程所有权

启动 Chromium 后记录：

- 根浏览器 PID（能从 DrissionPage/调试端口解析时）
- 调试端口
- `user_data_path`
- 当前 CLI PID

正常关闭先调用 `quit(force=True, del_data=True)`，轮询确认拥有的根 PID 已退出；超时才按已记录 PID 终止进程树。不得按进程名、模糊窗口标题或全局 `pkill` 清理浏览器。

## 测试策略

### 邮箱

- 使用本地假 HTTP 服务验证创建请求的路径、headers、body 和响应映射。
- 验证 `/api/parsed_mails` 的 JWT/站点密码、验证码提取、429 退避和 404 raw 回退。
- 验证地址 JWT 删除及 admin id 回退。
- 验证旧配置迁移和其他邮箱提供商不受影响。

### UI/API

- Flask test client 验证配置 GET/POST、必填校验、秘密字段保存和旧字段迁移。
- 浏览器检查 provider 切换、字段显隐、保存后回填和错误提示。

### 浏览器与进程

- 使用假浏览器对象验证正常轮次只重置、不重启；失联时只重启一次；最后一轮不额外启动。
- 使用假子进程日志验证父进程按轮次刷新硬超时、统计结果和断点续跑。
- Windows 实测连续启动/重置/关闭若干次，记录项目前后 `autoPortData` 浏览器根进程数；结束后应回到基线。
- 验证清理函数不会匹配普通用户 Chrome/Edge。

### 发布门槛

- 新增聚焦测试全部通过。
- 完整 pytest 通过。
- `python -m compileall` 通过。
- Web 面板可启动且关键配置 UI 经真实浏览器验证。
- Git diff 无空白错误。
- `git status` 不包含凭据或运行产物。
- GitHub 仓库为 PUBLIC，远端 `master` 与本地 HEAD 一致。

## 发布与归属

保留 `lingxiaoyiyu-hub/grok-register-win` 的完整 Git 历史和 MIT 许可证。README 更新仓库徽章、链接和新增功能说明，并增加上游归属与本次参考实现链接。新远端命名为 `aiis2`，上游远端保留为 `upstream`。
