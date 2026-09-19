# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

gapi 是 FreeLLM API（Node/Express 上游，默认 :3001）的**二级业务网关**（FastAPI，:3002）：面向终端用户提供注册登录、签发 `gapi-` 前缀的 API Key、代理转发、按 GavinCoin（◎）计量扣费。

硬约束（来自 `docs/design.md`，违反即架构错误）：

- 独立 Python 项目，**不 import、不修改** 父目录 `server/`、`client/`、`shared/` 下任何代码；SQLite 库（`data/gapi.db`）与上游库完全隔离。
- 管理分工：`manage.py` CLI 管充值/定价/启停；`/admin/*` 管经济参数与公告；`/users/*` 管用户 CRUD（均为 admin 角色）。上游供应商管理永远留在 FreeLLM dashboard，gapi 不做。
- `FREELLM_API_KEY` 只存在于环境变量；转发时剥除用户的 `Authorization`/`x-api-key`/`x-goog-api-key`，统一注入它。
- 流式逐 chunk 转发，绝不在内存累积完整响应。
- `docs/design.md` 是定稿的设计蓝图（含上游协议侦察结论），改计费/转发行为前先查它。

## 常用命令

```bash
# 初始化（Python ≥ 3.11）
pip install -e '.[dev]'
cp .env.example .env          # 必填 FREELLM_API_KEY、GAPI_JWT_SECRET
alembic upgrade head
python manage.py pricing seed # 写入默认定价

# 运行（同时提供 API 和用户面板，面板在 /）
uvicorn app.main:app --port 3002 --reload

# 测试（必须先设 PYTHONPATH；pytest-asyncio 为 auto 模式）
PYTHONPATH=$(pwd) .venv/bin/pytest -q
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase3_billing.py -q                    # 单文件
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase3_billing.py::test_concurrent_reserves_cannot_overdraw -q  # 单用例

# 数据库迁移
alembic revision --autogenerate -m "..." && alembic upgrade head

# 管理 CLI（coin/user/pricing/usage 四组，见 python manage.py --help）
python manage.py coin grant <email> <amount> --note "..."
python manage.py pricing set <model> --input <rate> --output <rate>
```

## 架构要点

分层：**routers（HTTP）→ services（业务）→ models（DB）**。同步 SQLAlchemy 2.0 + SQLite WAL，DB 结算经 `anyio.to_thread` 避免阻塞事件循环。

**两种认证**：面板/账户接口用 JWT（HS256，注册即返回，注册直接进面板；同时写 HttpOnly + SameSite=Strict 的 `gapi_session` cookie）；`/v1/**`、`/v1beta/**` 转发面用 gapi Key（SHA256 哈希存储，明文仅创建时返回一次，`expires_at` 会被强制）。首个注册用户自动成为 admin。

**指纹绑定（多设备集合）**：`user_fingerprints` 表，每账号最多 5 个绑定（LRU 驱逐）。威胁模型是**防 token/cookie 被盗后无密码裸用**，不是登录屏障——登录/注册有密码背书，新环境一律自动登记（`app/services/fingerprint.py`）；**面板 API（无密码场景）才严格**：已绑定账号必须携带集合内任一 `X-Fingerprint` 头，缺失或不匹配 403。gapi Key 面不查指纹。前端 `fp.js` 只采集稳定信号（窗口尺寸/缩放/availWidth/DPR 等漂移源已移除）；playground 流式 fetch 必须经 `ctx.getFingerprint()` 注入该头。逃生通道：CLI `manage.py user clear-fingerprint <email>` 或 admin 面板「解绑指纹」按钮（`DELETE /users/{uid}/fingerprints`）。

**管理面有三个入口，分工固定**：`manage.py` CLI（充值、定价、启停账号、兑换码、用量报告）；`/admin/*`（经济参数 settings、公告 CRUD、全局统计 stats，admin）；`/users/*`（用户 CRUD，admin）。**不要**再给同一功能开第四个面。经济参数（coin_rate、daily_bonus、registration_bonus、max_coin_per_user、registration_open、rate_multiplier）的生效优先级是 **DB 行 > .env > 硬编码**（`app/services/coin.py` 的 `DEFAULTS` 从 env Settings 派生，60s TTL 缓存，写入即失效）——注册奖励、购包汇率、`/config` 都必须走 `get_settings(db)` 读。`.env` 路径锚定项目根（`app/config.py` 的 `_ENV_FILE`），与进程 CWD 无关；改 `.env` 需重启进程（uvicorn --reload 不监控它）。

**缓存纪律**：`system_settings` 与 `pricing` 都有进程内 TTL 缓存（60s），**只缓存纯值，绝不缓存 ORM 实例**（session 关闭后访问即 DetachedInstanceError）。测试由 conftest 的 autouse fixture 自动失效三个缓存（settings/pricing/ratelimit），直接 `db.add` 写 pricing/settings 的测试无需手动处理。

**计费流（`app/services/billing.py`）**：请求前按 `max_tokens` 估算预扣（向上取整，`BEGIN IMMEDIATE` 事务保证并发不超扣）→ 余额不足在转发前返回 402（上游从不返回 402，402 是 gapi 专属）→ 响应后按实际 usage 结算、差额退回。费率按模型的 input/output per-1k 定价，未定价模型回退 gpt-4o 费率（宁可多收不免费）。Token 计数优先上游 usage 字段，缺失才用 tiktoken 估算。客户端断流时关闭上游连接，按已产生 token 结算并记 `interrupted` 状态；上游连接失败/4xx-5xx 时预扣全额退回（`charge_flat=False`）。

- **媒体端点按次计费**：images/videos/audio 无 token 流，`reserve(flat_fee=…)` 预扣 `pricing.flat_fee_coin`（缺省端点基础价 images ◎0.05 / videos ◎0.5 / audio ◎0.02，**再乘 rate_multiplier**），CLI `pricing set --flat-fee` 可覆盖。
- **结算重定价**：`settle(rates_override=…)` 按上游 `X-Routed-Via` 返回的实际模型费率结算（设计 D6）；`model=auto` 不会按便宜模型价收贵模型的账。
- **全局计费倍率**：`rate_multiplier` 设置在 `get_rates`/`get_flat_fee` 出口处整体缩放按量计费（试验场与 API 立即生效）；`coin_rate` 只管购包兑换，两者作用域不同，改文案时别混淆。
- **并发槽位**：`proxy.py` 的 `_acquire_slot/_release_slot` 用带条件的原子 UPDATE（不是读-改-写）；流式请求的槽位由响应生成器持有到流结束才释放。

**错误契约（不可破坏）**：gapi 自身错误统一 `{"error": {"code", "message"}}` 且带 `X-Gapi-Error: 1` 响应头——`app/main.py` 专门重写了 FastAPI 的 422/404/405/500 处理器来遵守它；上游错误体**原样透传不加该头**。客户端据此区分网关拒绝与上游返回。

**路由注册顺序有功能意义**（`app/main.py`）：auth/keys/user/admin/users → panel → playground → **proxy 最后**（其 `/v1/{path:path}` 通配不能吞掉面板路由）→ 静态 `web/public` 挂在 `/`。**`StaticFiles` 挂在 `/` 会吞掉一切未完全匹配的路径**（Starlette 的 slash 重定向兜底在它之后，永远到不了）：所以 API 路由必须用 `@router.get("")` 而非 `"/"`，路径里不要依赖尾斜杠重定向。

**转发面（`app/routers/proxy.py`）**：`ROUTES` 表列出可转发端点（chat/completions、embeddings、messages、responses、images/videos/audio、v1beta/*），未列出的 404 `endpoint_not_forwarded`。`usage_parser.py` 把 4 种线协议（OpenAI chat SSE、Anthropic SSE、Responses 双行帧、Gemini usageMetadata）的 usage 归一化。仅对非流式请求在连接错误/5xx 时重试 1 次（不重试 429；上游内部已有 20 跳 fallback，避免双倍重试）。

**模型目录（`app/services/catalog.py`）**：每 300s 拉一次上游 `/v1/models`（single-flight + 失败回退旧缓存），`/user/models?force=1` 可强制刷新。**已知设计差距**：设计文档 D5 的 `GAPI_VISIBLE_PLATFORMS` 平台白名单 fail-closed 过滤未实现，当前可见性完全信任上游目录字段。

**公告系统**：`Announcement.is_active` 是存储的开关列（面板 toggle 走 `PATCH /admin/announcements/{aid}`），`active`/`live` 属性 = 开关 AND 时间窗口。用户端弹窗在 `panel.js`，纯文本公告必须先 `esc()` 再拼 HTML；`as_html=true` 是授信 admin 的原始 HTML 通道（CSP 兜底）。

**用户面板（`web/`）**：零构建原生 HTML/CSS/JS，`web/public` 是未登录壳，`web/panel` 由 `app/routers/panel.py` 在会话检查后下发。改完刷新即可，无打包步骤。CSP 严格（无 inline script、仅 self），加前端功能时不要引入外部 CDN。静态资产用 `?v=` 版本串防缓存错位，panel 资产响应头 `no-cache`（ETag 重新验证）。主题跟随 `prefers-color-scheme`（亮/暗双套 CSS 变量，新增颜色必须走变量）。导航折叠是**设备驱动**：`isMobileEnv()`（UA + userAgentData + 触屏）判手机一律折叠，`body.is-mobile` 同时折叠次要表格列（`.col-opt`）；桌面走 `fitNav()` 实测宽度，放得下不折。

**限流与兑换码**：登录/注册/重发验证有内存滑动窗口限流（`app/services/ratelimit.py`，单实例适用）；兑换码走 `billing.redeem()`（`BEGIN IMMEDIATE` + 每用户一次 + max_uses 上限），CLI `manage.py code create/list/deactivate` 生成。新面板 tab 需要同时登记三处：`panel.html` 的 nav+section、`panel.py` 的 `_ASSETS`、`panel.js` 的 `TABS`（admin tab 另加 `ADMIN_TABS`）。

## 测试注意事项

- `tests/conftest.py` **在 import app 之前**设置 `GAPI_DATABASE_URL`（临时目录）、`GAPI_JWT_SECRET`、`GAPI_EMAIL_ENABLED=false`——settings 有缓存，新测试文件若需环境变量必须同样前置。
- 测试用 httpx `ASGITransport` 打 gapi、`MockTransport` mock 上游；上游协议行为（SSE 帧格式、usage 位置）以 `docs/design.md` §2.2 的侦察结论为准。
- 测试文件按实施阶段命名（`test_phase1_*` … `test_phase5_*`），计费并发的关键回归是 `test_phase3_billing.py::test_concurrent_reserves_cannot_overdraw`。
