# gapi — FreeLLM API 二级业务网关

面向终端用户的 LLM 网关：注册登录、签发 `gapi-` 前缀 API Key、代理转发到
FreeLLM API、按 **GavinCoin（◎）** 计量扣费。FastAPI + SQLite + 零构建前端，
单实例即可运行。日常运维（充值、定价、停用账号）走 `manage.py` CLI；经济参数、
用户与公告管理在面板的 admin 标签页（`/admin/*`、`/users/*`，仅 admin 角色可用）。

`gapi/` 是独立的 Python 项目，数据库 (`data/gapi.db`) 与 `server/data/freellmapi.db`
完全隔离，不 import 也不修改 `server/`、`client/`、`shared/` 下任何代码。

架构定位、完整设计与演进记录见 **[docs/design.md](docs/design.md)**
（架构定稿，与实现同步，含上游协议侦察结论）；
交付说明与运维备忘见 **[docs/delivery-2026-09-19.md](docs/delivery-2026-09-19.md)**。

```
用户 ──gapi Key──▶ gapi(:3002) ──注入 FREELLM_API_KEY──▶ FreeLLM API(:3001)
  │                  │
  └──浏览器面板──────┘   注册/登录/JWT · 密钥 · 账单 · 公告 · 试验场 · 管理
```

## 功能一览

- **计费**：预扣 → 结算 → 退款（`BEGIN IMMEDIATE` 原子，并发不超扣）；模型级
  per-1k 费率表 + 全局计费倍率 + 媒体按次价；按上游 `X-Routed-Via` 实际路由模型
  重定价；上游失败全退；代币包（订阅式配额，FIFO 扣减）与兑换码充值。
- **安全**：多设备指纹绑定（≤5，登录自动登记、面板 API 严格校验、CLI/网页可解绑）、
  登录/注册限流、key 过期强制、HttpOnly+SameSite 会话、严格 CSP、统一错误契约
  （`X-Gapi-Error: 1` 区分网关拒绝与上游透传）。
- **用户面板**（零构建）：概览/试验场/模型/密钥/账单/用量，汉堡式自适应导航，
  亮暗主题跟随系统，移动端表格折叠。
- **管理**：CLI（充值/定价/兑换码/启停/用量）+ 面板 admin 标签页（经济参数、
  公告、全局统计、用户管理）。首个注册用户自动 admin。

## 快速开始

```bash
# 前置：上游 FreeLLM API 在跑（cd freellmapi && docker compose up，或 server/ npm run dev）

cd freellmapi/gapi
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env            # 必填 FREELLM_API_KEY、GAPI_JWT_SECRET
alembic upgrade head
python manage.py pricing seed   # 写入默认定价
uvicorn app.main:app --port 3002 --reload
```

打开 <http://localhost:3002/> 即登录/注册页（首个注册用户自动 admin），
`GET /health` 为健康检查。数据库自动建在 `data/gapi.db`（WAL）。

> **配置优先级**：管理面板/`system_settings` 表 > `.env` > 内置默认。
> 改 `.env` 需重启进程（`--reload` 不监控它）；面板改经济参数立即生效。

## HTTP 面（速览）

| 端点 | 认证 | 说明 |
|---|---|---|
| `POST /auth/register` `login` | — | 限流；注册受 `registration_open` 门控，返回 JWT 直接进面板；登录自动签到 |
| `POST /user/checkin` | JWT | 每日签到（24h 滚动窗口，面板加载时自动调用） |
| `POST /auth/resend-verification` `GET /auth/verify-email` | JWT / token | 邮箱验证（3 次/小时） |
| `GET/POST/DELETE /user/keys` | JWT | Key 增删查；列表带 30 天用量；明文仅创建时显示一次 |
| `POST /user/redeem` | JWT | 兑换码充值（GAVI-…） |
| `POST /user/password` | JWT | 修改密码（需旧密码） |
| `GET/POST /user/packages` `GET /user/balance` `transactions` `dashboard` `usage(/models,/recent)` `models` `routing` `announcements` | JWT | 面板数据 |
| `POST /user/playground/chat` | JWT | 试验场（走完整代理+计费管线） |
| `GET/PUT /admin/settings` `GET /admin/stats` `CRUD /admin/announcements` | admin | 经济参数 / 全局统计 / 公告 |
| `CRUD /users` `DELETE /users/{uid}/fingerprints` | admin | 用户管理 / 解绑指纹 |
| `POST /v1/{path}` `GET /v1/models` `GET,POST /v1beta/{path}` | gapi Key | 转发面（chat/completions、embeddings、messages、responses、images/videos/audio、v1beta） |
| `GET /panel /panel/assets/*` | JWT（宽松） | 面板壳/资产，allow-list 防穿越 |
| `GET /health /config` | — | 健康检查 / 登录前公开配置（bonus/tokenRate 读 live 设置） |
| `GET /` | — | 登录壳（静态） |

**错误契约**：gapi 自身错误统一 `{"error": {"code", "message"}}` 且带
`X-Gapi-Error: 1`；上游错误体原样透传**不带**该头——客户端据此区分网关拒绝与
上游返回。402 只会来自 gapi（余额不足），upstream 从不返回 402。

## 计费

```
按量：cost = tokens/1000 × 模型费率(in/out 分开) × rate_multiplier
媒体：cost = 端点基础价(images ◎0.05 / videos ◎0.5 / audio ◎0.02) × rate_multiplier
```

- 请求前按 `max_tokens` 预扣上限（向上取整），余额不足在**转发前**返回 402；
- 响应后按实际 usage 结算、差额退回；上游连接失败/4xx-5xx 全额退；
- 客户端断流按已产生 token 结算（状态 `interrupted`）；
- 未定价模型回退 gpt-4o 费率（宁可多收不免费）；token 计数优先上游 usage，
  缺失才用 tiktoken 估算；
- 每笔余额变动都落 `credit_transactions` 账本（正充负扣、balance_after）。

经济参数（面板「设置」或 `system_settings`）：`rate_multiplier`（计费倍率）、
`coin_rate`（配额兑换率，仅购包）、`daily_bonus`、`registration_bonus`、
`max_coin_per_user`（免费额度封顶）、`registration_open`（关闭公开注册）。

## CLI（`python manage.py` 或 `gapi-manage`）

```bash
python manage.py coin grant <email> <amount> [--note]   # 充值（负数为扣）
python manage.py coin balance <email>
python manage.py user list | activate | deactivate <email>
python manage.py user fingerprints <email>              # 查看指纹绑定
python manage.py user clear-fingerprint <email>         # 解锁被指纹绑定的账号
python manage.py pricing set <model> --input <r> --output <r> [--flat-fee <f>]
python manage.py pricing list | seed | sync [--dry-run] # sync 批量补未定价模型
python manage.py usage user <email> [--from YYYY-MM-DD] [--to YYYY-MM-DD]
python manage.py code create <amount> [-n N] [--uses N] [--note "..."]   # 兑换码
python manage.py code list [--active] | deactivate <CODE>
```

## 测试

```bash
PYTHONPATH=$(pwd) .venv/bin/pytest -q                                # 全量（101 项）
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase3_billing.py -q    # 单文件
```

上游协议行为（SSE 帧格式、usage 位置）以 `docs/design.md` §11 的侦察结论为准；
关键回归：计费并发、错误契约、指纹语义、兑换码防超兑、面板防穿越、
每日签到防双领。

## 配置（`.env`）

| 变量 | 说明 |
|---|---|
| `FREELLM_API_BASE` / `FREELLM_API_KEY` | 上游地址与 unified key（`freellmapi-…`） |
| `GAPI_JWT_SECRET` | 会话签名密钥（生产必须换长随机串，默认值启动时告警） |
| `GAPI_DATABASE_URL` | 默认 `sqlite:///./data/gapi.db` |
| `GAPI_TOKEN_PACKAGE_RATE` | 配额兑换率兜底（DB 行优先） |
| `GAPI_REGISTRATION_BONUS` | 注册赠送兜底（DB 行优先） |
| `GAPI_UPSTREAM_TIMEOUT_CHAT/EMBEDDINGS/MEDIA` | 上游超时（300/60/300 秒） |
| `GAPI_DEFAULT_CURRENCY` | 显示货币名（GavinCoin） |
| `SMTP_HOST/PORT/USER/PASSWORD` `EMAIL_SENDER` | 验证邮件（端口 465 走 SMTPS） |
| `GAPI_EMAIL_ENABLED` | `false` 关闭外发邮件（注册不依赖邮件可达） |
| `BASE_URL` | 验证邮件里的链接域名 |

> `.env` 路径锚定项目根，与启动目录无关；改完需重启进程。

## 目录

```
app/          FastAPI 应用（routers → services → models 分层）
cli/          manage.py 本体（coin/user/pricing/usage/code 五组命令）
web/          零构建前端：public（登录壳）+ panel（会话面板，会话门控下发）
alembic/      迁移（单链，head = c3d4e5f6a7b8）
tests/        phase1–10 共 97 项测试
docs/design.md  架构定稿（演进记录 + 上游协议侦察）
```
