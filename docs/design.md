# gapi 架构定稿（与实现同步）

> FreeLLM API 的二级业务网关（用户侧）。初版设计于 2026-09-11，
> 本文档于 2026-09-18 全面改写：初版蓝图已基本实现并经全量审计与功能演进，
> 本文描述的是**当前实际运行的系统**；与初版的差异记录在 §9「演进记录」。

## 1. 定位与硬约束

FreeLLM API（Node/Express，本机 :3001，下称 **upstream**）是单用户免费 LLM 聚合网关，
持有全部上游供应商密钥与统一推理密钥 `freellmapi-…`。gapi（FastAPI，:3002）是面向
多终端用户的二级网关：

- 用户持 **gapi 自有 Key**（`gapi-` 前缀，SHA-256 哈希存储）访问；
- gapi 校验后剥除用户认证头，**替换**为环境变量 `FREELLM_API_KEY` 转发 upstream；
- 按 **GavinCoin（◎）** 计量扣费（预扣 → 结算 → 退款，见 §5）；
- 用户自助：面板（`web/`）+ REST API；管理员：`manage.py` CLI + 面板 admin 标签页。

硬约束（违反即架构错误）：

1. 独立 Python 项目，**不 import、不修改** 父目录 `server/ client/ shared/` 任何代码；
2. SQLite 库（`data/gapi.db`）与 upstream 库完全隔离；
3. `FREELLM_API_KEY` 只存在于 gapi 环境变量，永不下发给客户端；
4. 流式逐 chunk 转发，绝不在内存累积完整响应；
5. 上游供应商管理永远留在 FreeLLM dashboard，gapi 不做；
6. 管理面分工固定（§7）：CLI / `/admin/*` / `/users/*`，不开第四个面。

## 2. 分层与目录

**routers（HTTP）→ services（业务）→ models（DB）**。
同步 SQLAlchemy 2.0 + SQLite WAL（busy_timeout=5000）；计费事务走
`BEGIN IMMEDIATE`（见 §5）。

```
gapi/
├── manage.py                     # CLI 薄入口（真身在 cli/manage.py，即 gapi-manage 脚本）
├── alembic/versions/             # 0001…c3d4e5f6a7b8 单链迁移
├── app/
│   ├── main.py                   # 路由注册顺序有功能意义（见 §6）；安全头/CSP；错误契约
│   ├── config.py                 # pydantic-settings；.env 锚定项目根
│   ├── database.py  deps.py  errors.py  security.py
│   ├── models/                   # 11 张表（§4）
│   ├── schemas/                  # auth.py、key.py（含金额 str 序列化约定）
│   ├── routers/
│   │   ├── auth.py  keys.py  user.py  admin.py  users.py
│   │   ├── panel.py              # 会话门控下发面板壳/资产（allow-list）
│   │   ├── playground.py         # JWT 试验场，复用代理管线
│   │   └── proxy.py              # 转发内核：ROUTES、并发槽位、重试、流式、结算
│   └── services/
│       ├── billing.py            # 计费核心（§5）
│       ├── coin.py               # 经济参数（DB > .env > 硬编码，60s 缓存）
│       ├── fingerprint.py        # 多设备指纹集合（§8）
│       ├── catalog.py            # 300s 上游模型目录（single-flight + 旧缓存兜底）
│       ├── ratelimit.py          # 进程内滑动窗口限流
│       ├── headers.py  upstream.py  usage_parser.py  token_counter.py
│       └── email.py
├── cli/                          # manage.py 本体 + commands/{coin,user,pricing,usage,code}
├── web/                          # 零构建前端：public（登录壳）+ panel（会话面板）
├── tests/                        # test_phase1_* … test_phase10_*（97 项）
└── seed/tiers.json               # 初版 tier 方案遗留，仅 scripts/export_tiers.py 引用
```

## 3. 认证模型

| 面 | 凭证 | 说明 |
|---|---|---|
| 面板/账户 API | JWT（HS256，7 天）+ `gapi_session` HttpOnly Cookie（SameSite=Strict） | 注册即返回 JWT 并直接进面板 |
| 转发面 `/v1/**` `/v1beta/**` | gapi Key（SHA-256 存储，明文仅创建时显示一次） | `Bearer` / `x-api-key` / `x-goog-api-key` 三种取法；`expires_at` 强制 |
| 管理面 `/admin/*` `/users/*` | JWT + `role == "admin"` | 首个注册用户自动 admin |

**指纹绑定（多设备集合）**：`user_fingerprints` 表，每账号最多 5 个绑定（LRU 驱逐）。
威胁模型是**防 token/cookie 被盗后无密码裸用**，不是登录屏障：

- 登录/注册（密码背书）：新环境**自动登记**，永不锁死登录；
- 面板数据 API（无密码场景）：必须携带集合内任一 `X-Fingerprint` 头，缺失/不匹配 403；
- 面板壳与 JS 资产（浏览器导航，无法带自定义头）：仅 JWT 检查（宽松变体 `get_current_user_lenient`）；
- gapi Key 面不查指纹（SDK/curl 从不发送）；
- 逃生通道：`manage.py user clear-fingerprint <email>`、`DELETE /users/{uid}/fingerprints`。

**限流**（进程内滑动窗口，单实例适用）：登录 30 次/5 分钟/IP、注册 20 次/小时/IP、
重发验证 3 次/小时/账号；超限 429 `rate_limited`（带 `X-Gapi-Error: 1`）。

## 4. 数据表（11 张）

| 表 | 要点 |
|---|---|
| `users` | role(user/admin)、gavincoin_balance Numeric(18,6)、concurrent_limit/active、email_verified、last_login_date、last_announcement_at、routing_strategy |
| `api_keys` | key_hash unique、key_prefix、quota(预留语义，未消费)、expires_at（强制） |
| `user_fingerprints` | (user_id, fp_hash) unique；created_at / last_seen_at |
| `email_verifications` | uuid token、24h 有效；无 DB 级 ON DELETE → ORM cascade 兜底 |
| `token_packages` | 订阅式配额：total/remaining_tokens、cost_gavincoin、expires_at（FIFO 扣减、过期即弃） |
| `usage_records` | prompt/completion/total/package_tokens、gavincoin_cost、duration_ms、status(ok/error/interrupted/upstream_error)、request_id |
| `credit_transactions` | 账本：正充负扣、balance_after、tx_type(reserve/settle/grant/adjust/package_purchase/daily_login/signup_bonus/redeem)、reference_id（有索引） |
| `pricing` | model unique、input_per_1k、output_per_1k、flat_fee_coin（媒体按次价） |
| `redemption_codes` | code（GAVI-XXXX-XXXX-XXXX，无 0/O/1/I）、amount、max_uses/used_count、is_active |
| `announcements` | title/content、as_html、**is_active（存储开关）**、start/end 窗口、priority；`live` = 开关 AND 窗口 |
| `system_settings` | 经济参数（key unique）；见 §5 优先级 |

## 5. 计费模型

### 5.1 三个价格杠杆（作用域不同，勿混淆）

1. **模型定价表**（`pricing`）：per-1k 基础费率 + 媒体按次价；未定价模型回退
   gpt-4o 费率（0.005/0.015 ◎/1k，宁可多收不免费）。CLI：`pricing set/seed/sync`。
2. **计费倍率** `rate_multiplier`（system_settings）：`get_rates`/`get_flat_fee`
   出口整体缩放**按量计费**（试验场 + API，含媒体），改后立即生效。
3. **配额兑换率** `coin_rate`（system_settings，env 桥接 `GAPI_TOKEN_PACKAGE_RATE`）：
   **只管购包**——◎1 = coin_rate × 1000 tokens 的周期配额（批量价）。

其他经济参数：`daily_bonus`（每日签到奖励，受 `max_coin_per_user` 封顶）、
`registration_bonus`（env 桥接 `GAPI_REGISTRATION_BONUS`）、`registration_open`。

### 5.3 每日签到（24h 滚动窗口）

`coin.award_daily_bonus(db, user)` 是登录与面板自动签到共用的原子入口：

- 窗口是**滚动 24h**（`now - last_login_date >= 24h`），不是自然日——跨日不补领；
- 签到标记写在 `users.last_login_date`，**不写日历日**；
- 整个 claim 是一次带条件的 UPDATE（`last_login_date < cutoff`）包在
  `BEGIN IMMEDIATE` 里：双击、双标签页、登录与面板同时触发，**不可能双领**；
- 余额受 `max_coin_per_user` 封顶，达到上限时 `granted=0`（仍写 `last_login_date`
  以推进窗口，但不落账本）；
- 返回值：`None` = 窗口内已领过；`Decimal` = 本次实发（含 0）。

调用方：`POST /auth/login`（密码背书）、`POST /user/checkin`（面板加载时自动调用）。
面板 JS 在 boot 里调用一次，签到成功弹轻提示；失败静默忽略。

**生效优先级：DB 行 > `.env` > 硬编码兜底**；60s TTL 缓存，`set_setting` 写入即失效；
改 `.env` 需重启进程（uvicorn --reload 不监控它）。缓存只存纯值，**绝不缓存 ORM 实例**。

### 5.2 预扣 → 结算 → 退款

```
cost = tokens/1000 × 费率（input 与 output 分开计）× rate_multiplier
```

- `reserve()`：按 `max_tokens` 估算预扣（向上取整），`BEGIN IMMEDIATE` 内原子执行：
  代币包先扣（FIFO、过期弃），未覆盖部分查 `balance >= amount` 后扣款；不足 402
  （**在转发之前**；402 是 gapi 专属，upstream 从不返回 402）。
- `settle()`：`BEGIN IMMEDIATE` 内释放预扣 → 按实际 usage 重画包 → 差额退回/补扣，
  写 usage_record + 账本。结算按 **`X-Routed-Via` 实际路由模型**重新定价（设计 D6）。
- 失败路径：上游连接失败 / 4xx-5xx → 预扣全额退（`charge_flat=False`）；
  客户端断流 → 按已产生 token 结算，状态 `interrupted`。
- 媒体端点无 token 流：`reserve(flat_fee=…)` 预扣按次价，成功才收，失败全退。
  端点基础价 images ◎0.05 / videos ◎0.5 / audio ◎0.02（×倍率）。
- 并发槽位：`_acquire_slot/_release_slot` 带条件原子 UPDATE；流式请求的槽位由
  响应生成器持有到流结束。
- 兑换码 `redeem()`：同 BEGIN IMMEDIATE 纪律 + 每用户一次 + max_uses 上限。

## 6. HTTP 面与路由顺序

路由注册顺序有功能意义：auth/keys/user/admin/users → panel → playground →
**proxy 最后**（`/v1/{path:path}` 通配不能吞面板路由）→ 静态 `web/public` 挂 `/`。

**注意**：`StaticFiles` 挂在 `/` 会吞掉一切未完全匹配的路径（Starlette 的尾斜杠
重定向兜底在它之后，永远到不了）——API 路由必须用 `@router.get("")` 而非 `"/"`。

| 端点 | 认证 | 说明 |
|---|---|---|
| `POST /auth/register` / `login` | — | 限流；register 受 `registration_open` 门控；登录自动触发每日签到 |
| `POST /user/checkin` | JWT | 每日签到（24h 滚动窗口，面板加载时自动调用） |
| `GET /auth/me` `POST /auth/logout` `GET /auth/verify-email` `POST /auth/resend-verification` | JWT | 验证邮件限流 3/h |
| `GET/POST/DELETE /user/keys` | JWT | 列表带 30 天用量（requests_30d/tokens_30d） |
| `POST /user/redeem` | JWT | 兑换码充值 |
| `POST /user/password` | JWT | 改密码（需旧密码） |
| `GET /user/balance·packages·transactions·dashboard·usage(/models,/recent)·models·announcements·routing` | JWT | 面板数据；`/user/models?force=1` 强刷目录 |
| `POST /user/packages` | JWT | 购包（coin_rate 定兑换比） |
| `POST /user/playground/chat` | JWT | 试验场，走完整代理+计费管线 |
| `GET/PUT /admin/settings` | admin | 经济参数批量原子更新 |
| `GET /admin/stats` | admin | 全局统计（30 天请求/消耗、7 天趋势、Top 模型/用户） |
| `CRUD /admin/announcements(/active)` | admin | 公告；toggle 走 `PATCH {is_active}` |
| `CRUD /users(/{uid})` `DELETE /users/{uid}/fingerprints` | admin | 用户管理；改邮箱 lower+strip |
| `POST /v1/{path} GET /v1/models` `GET,POST /v1beta/{path}` | gapi Key | 转发面（ROUTES 白名单） |
| `GET /panel /panel/assets/*` | JWT（宽松） | 面板壳/资产，allow-list 防穿越 |
| `GET /health /config` | — | 健康检查 / 登录前公开配置（bonus/tokenRate 读 live 设置） |
| `GET /` | — | 登录壳（静态） |

**错误契约（不可破坏）**：gapi 自身错误统一 `{"error": {"code", "message"}}` 且带
`X-Gapi-Error: 1`——main.py 重写了 FastAPI 的 422/404/405/500 处理器；上游错误体
**原样透传不加该头**。客户端据此区分网关拒绝与上游返回。

## 7. 管理面分工（固定三入口）

| 入口 | 职责 |
|---|---|
| `manage.py`（即 `gapi-manage`） | coin grant/balance/history；user list/activate/deactivate/fingerprints/clear-fingerprint；pricing set/list/seed/sync；usage user；code create/list/deactivate |
| `/admin/*`（面板「统计/设置/公告」标签） | 经济参数、公告 CRUD、全局统计 |
| `/users/*`（面板「用户」标签） | 用户 CRUD、充值（grant）、解绑指纹、删除（唯一 admin 保护 `role=="admin"` SQL 判断） |

## 8. 前端（零构建）

`web/public`（登录壳）+ `web/panel`（会话面板，`panel.py` 会话门控 + allow-list 下发）。
原生 HTML/CSS/JS，无打包，改完刷新即生效。

- **主题**：跟随 `prefers-color-scheme`，亮/暗两套 CSS 变量（`:root` + light 覆盖）；
  新增颜色必须走变量，硬编码色一律收编。
- **导航折叠（设备驱动）**：`isMobileEnv()`（userAgentData.mobile / UA / iPadOS 触屏）
  判手机一律折叠汉堡；桌面 `fitNav()` 实测宽度——放得下全部按钮就完整横排，
  放不下才折（admin 10 标签与普通用户 7 标签折叠宽度不同）。`body.is-mobile`
  同时折叠表格次要列（`.col-opt`，如「最近请求」的端点/状态列）。
- **缓存**：静态资产 URL 带 `?v=` 版本串（改前端时整体升档），面板资产响应头
  `no-cache`（ETag 重新验证，304 省流量）；`panel.html` no-store。
- **安全**：CSP 严格（script-src 'self'、无 inline script）、纯文本公告渲染前必须
  `esc()`、`as_html=true` 是授信 admin 的原始 HTML 通道（CSP 兜底）；密钥明文用
  textContent 注入；表单/表格在窄屏自动折行/横滚。
- **管理员标签默认隐藏（防慢网泄露）**：`panel.html` 中 4 个 admin nav 按钮
  **自带 `hidden` 类**，`boot` 里 `/auth/me` 确认 `role == "admin"` 后才移除；
  `panel.js` 另有 `isAdminUser` 点击守卫——非管理员连 `openTab` 都不会触发。
  三层防线：HTML 默认隐藏（慢网络/JSError 也不泄露）→ JS 点击守卫 → 服务端
  `get_admin_user` 403（真正的闸门）。切勿把 admin 按钮改回默认可见。
- 新面板 tab 需登记三处：`panel.html` nav+section、`panel.py` `_ASSETS`、
  `panel.js` `TABS`（admin tab 另加 `ADMIN_TABS`）。

## 9. 演进记录（初版蓝图 → 现状）

| 初版决策 | 现状 | 说明 |
|---|---|---|
| D1/D2 档位评分制（Frontier=4…，输出 3× 加权） | per-模型 input/output per-1k 费率表 | 档位粒度太粗且偏离真实成本；CLI `pricing sync` 可批量补价 |
| D3 媒体 flat fee | 已实现（`flat_fee_coin` 列 + 端点基础价 × 倍率） | — |
| D4 首个用户 admin | 不变 | — |
| D5 `GAPI_VISIBLE_PLATFORMS` 平台白名单 fail-closed | **放弃**：信任上游目录字段（`available`/`execution_status`） | 白名单维护成本 > 收益；上游已反映密钥健康度。`seed/tiers.json` 与 `scripts/export_tiers.py` 为遗留工具 |
| D6 X-Routed-Via 重定价 | 已实现（`settle(rates_override=…)`） | — |
| 无 admin HTTP 界面 | 演进为三入口分工（CLI + `/admin/*` + `/users/*`） | CLI 管低频重操作，网页管高频轻操作 |
| 单指纹绑定（`users.fingerprint_hash`） | `user_fingerprints` 多设备集合（≤5，LRU） | 单哈希在窗口/缩放/UA 漂移下会锁死机主；且登录环节不再用指纹拦截（密码是更强背书） |
| Phase 1–5 分期计划 | 全部完成；审计修复 33 项（phase6）+ 兑换码/统计/倍率/限流/注册开关（phase7）+ 指纹集合（phase8）+ UX（phase9/10） | `review_phase1_findings.txt` 为审计遗留笔记，可删 |
| — | 新增：兑换码、全局统计、改密码、重发验证邮件、注册开关、计费倍率、per-key 用量、汉堡导航、亮暗主题 | — |
| 汉堡导航按钮默认全部可见 | admin nav 按钮默认 `hidden`，`/auth/me` 确认角色后才显示 + 点击守卫 | 慢网络下普通用户会在 JS 隐藏前看到并点开管理菜单；三层防线见 §8 |

## 10. 测试

`pytest + httpx ASGITransport（打 gapi）+ MockTransport（mock 上游）`，101 项，
按实施阶段命名：`test_phase1_auth/keys` → `phase2_proxy` → `phase3_billing` →
`phase4_panel_security` → `phase5_playground` → `phase6_audit`（审计回归）→
`phase7_features` → `phase8_fingerprint` → `phase9_ux_billing` → `phase10_panel_nav` →
`phase11_daily_checkin`。

关键不变量：计费并发（`test_phase3_billing.py::test_concurrent_reserves_cannot_overdraw`）、
错误契约（X-Gapi-Error 头）、面板资产防穿越、指纹语义（登录重绑/面板严格）、
兑换码防超兑。运行：`PYTHONPATH=$(pwd) .venv/bin/pytest -q`。

`tests/conftest.py` 在 import app 之前注入环境变量（临时库、测试密钥、
email 关闭、经济参数固定 100/20）——settings 有缓存，新测试文件必须同样前置。

## 11. 上游协议事实（侦察结论，仍然有效）

- 认证：`Authorization: Bearer` / `x-api-key` / `x-goog-api-key`（含 Gemini `?key=`）
  任一，同一 unified key 比对；gapi 剥除三者后统一注入 Bearer；query 中的
  `key`/`api_key` 在转发前剥离。
- 路由：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、
  `/v1/images|videos/generations`、`/v1/audio/speech|transcriptions`、`/v1/messages`
  （透传 `anthropic-version`/`anthropic-beta`）、`/v1/responses`、`GET /v1/models`、
  `/v1beta/models[/{model}:generateContent|streamGenerateContent[?alt=sse]|countTokens]`。
  gapi `ROUTES` 白名单外一律 404 `endpoint_not_forwarded`。
- SSE/JSON usage 位置：chat 终局 `choices:[]` usage 帧（缺失时带 `estimated:true`）、
  Anthropic `message_start`（input）+ `message_delta`（output）、Gemini
  `usageMetadata`（`?alt=sse` 逐块 JSON，无 alt=sse 为 JSON **数组**）、responses
  双行帧（usage 仅在 `response.completed`）。`?alt=sse` 与 `streamGenerateContent`
  视为流式。
- 上游已有最多 20 跳 fallback：gapi 仅对**非流式**请求在连接错误/5xx 重试 1 次
  （不重试 429）。
- 关键响应头透传：`X-Routed-Via`（计费重定价依据）、`X-Fallback-*`、`X-Provider`、
  `X-Model`、`X-Request-ID`、`Retry-After`。hop-by-hop 与 `server`/`date` 不透传。
- upstream 入站 body 上限 25MB、per-IP 限流 120rpm（gapi 与 upstream 同机时需调大
  或关闭）。

## 12. 已知边界（有意为之，非遗漏）

- gapi 侧无请求体大小上限（upstream 有 25MB）——先读入内存，超大请求靠 upstream 拒绝。
- `settle` 允许超出预扣的追加扣款（余额可为负）——上游实际 usage 大于 tiktoken
  预估时优先保服务可用，`test_phase3` 固定该行为。
- `ApiKey.quota` 列已存储但无消耗语义（语义未定义前不实现）。
- 限流/设置缓存为进程内存态：多实例部署需替换为共享后端（当前单实例架构）。
