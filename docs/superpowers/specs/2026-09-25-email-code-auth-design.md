# 邮箱验证码认证设计（注册收码 + 登录双轨）

日期：2026-09-25
状态：已获用户批准（设计评审通过，决策点 A/B/C 采纳推荐）

## 1. 背景与目标

现有邮件验证是注册后的**链接式**流程（`EmailVerification` token + `GET /auth/verify-email`），存在三个问题：

1. **发信静默失败**：`app/services/email.py` 的 `send_verification_email` 吞掉所有 SMTP 异常只记 warning。用户收不到邮件时注册/验证卡死且无任何提示——这是"邮件验证没搞好"的核心。
2. **登录与邮箱验证脱节**：`POST /auth/login` 不检查 `email_verified`，验证与否对登录无影响。
3. **无验证码形态**：用户期望的是国内平台标准的 6 位数字验证码体验。

目标（用户已确认）：

- **注册收码**：填邮箱+密码 → 发验证码 → 输码完成注册。新账号 100% 已验证，注册奖励当场发放。
- **登录双轨**：密码登录（不动）与邮箱验证码登录（免密，新增）并存。
- **单向发信**：服务器纯 SMTP 外发，不涉及任何收信基础设施（与现状一致，保持不变）。

## 2. 已确认决策

| 决策点 | 结论 |
|---|---|
| 验证形态 | 6 位数字邮箱验证码（免密登录） |
| 密码登录 | 并存（登录页双 tab） |
| 验证覆盖 | 注册收码 + 登录双轨 |
| 实现方案 | 方案 A：独立 `email_codes` 表 + 新端点，与 `EmailVerification` 老表并存 |
| A. 登录码遇未注册邮箱 | 404 `account_not_found`，前端引导去注册 tab。账号创建保持单一入口，所有账号都有密码（`password_hash` 非空约束不变） |
| B. 未验证老账号密码登录 | 放行 + 面板引导补验，不硬卡（避免锁死收不到信时期注册的老用户） |
| C. 重发验证 | `POST /auth/resend-verification` 改发验证码（purpose="verify"）；`GET /auth/verify-email` 仅为在途链接保留 |

## 3. 总体流程

### 3.1 注册（重排）

```
前端：邮箱+密码 → [发送验证码] → POST /auth/register/code/request
后端：限流 → registration_open → email_taken 检查 → 发码入库 → SMTP 发信
      （发信失败 → 作废该码 → 503 email_send_failed）
前端：输 6 位码 → 提交 POST /auth/register {email, password, code, fingerprint?}
后端：限流 → BEGIN IMMEDIATE → registration_open → email_taken → 原子验码消费
      → 建号（email_verified=True，注册奖励当场入账）→ 指纹登记 → commit
      → JWT + gapi_session cookie
```

注册奖励从 `verify-email` 端点挪回 `register`：验证码通过即证明邮箱所有权，与原"验证后才发奖励"的安全保证等价（原设计 T4 防的是"随机邮箱零摩擦领奖励"，验证码同样是摩擦点）。

### 3.2 登录双轨

- **密码登录**：`POST /auth/login` 完全不动（含指纹登记、每日签到）。
- **验证码登录**：
  ```
  POST /auth/login/code/request {email} → 发码（未注册 → 404 account_not_found）
  POST /auth/login/code/verify {email, code, fingerprint?}
      → 限流 → 原子验码消费 → 用户存在性/is_active 检查
      → email_verified 置 True（老未验证账号凭码补验——验证码即邮箱所有权证明）
      → 指纹登记（镜像密码登录：fingerprint_in_use 409 纪律相同）
      → commit（指纹先于签到，纪律见下）→ award_daily_bonus → JWT + cookie
  ```

### 3.3 老用户补验（面板内）

`POST /auth/resend-verification`（JWT）改发验证码（purpose="verify"）；新增 `POST /auth/verify-email-code`（JWT）验码后置 `email_verified=True`。`GET /auth/verify-email?token=` 保留不动，已发出的链接在 24h 窗口内仍可验证。

## 4. 数据模型

新表 `email_codes`（一个 alembic revision，`down_revision='d4e5f6a7b8c9'`）：

```python
class EmailCode(Base):
    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # register|login|verify
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA256 hex
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)      # naive UTC, +10min
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)            # 上限 5
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)      # naive UTC
```

约束与索引：

- `Index("ix_email_codes_email_purpose", email, purpose)`
- **不写** email+purpose 唯一约束：历史行保留作审计（谁何时请求过码）。请求新码时把同 email+purpose 的旧未消费行 `consumed_at=now` 作废（UPDATE，非 DELETE）。
- 时间列全链路 **naive UTC**（项目既有纪律：SQLite 时间比较 naive/aware 混用会 TypeError；`EmailVerification` 用 aware 是老表，新表不引入混用——查询比较时用 `datetime.utcnow()` 风格的 naive now）。

`app/models/__init__.py` 导出 `EmailCode`。

## 5. 服务层

### 5.1 新模块 `app/services/emailcode.py`

```python
CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5

def generate_code() -> str:
    """6 位数字码，secrets CSPRNG。"""

def issue_code(db, *, email: str, purpose: str, ip: str | None) -> str:
    """作废同 email+purpose 旧未消费码 → 插入新码（哈希）→ 返回明文码。
    调用方负责事务边界（BEGIN IMMEDIATE）与发信。"""

def consume_code(db, *, email: str, purpose: str, code: str) -> None:
    """原子验证+消费。抛 GapiError：
    - invalid_code (400/401 由调用方定)：无活码 / 码错 / 过期
    - 同一活码 attempts >= MAX_ATTEMPTS：作废该码后抛 invalid_code
    匹配成功：consumed_at=now。全程在调用方的 BEGIN IMMEDIATE 内，
    并发两个 consume 同码只有一个成功（写锁序列化，后到者看到 consumed）。"""
```

码哈希：`hashlib.sha256(code.encode()).hexdigest()`（与 gapi Key 同纪律：明文只出现在邮件里）。

### 5.2 改造 `app/services/email.py`

- 抽出 `_send_smtp(msg: EmailMessage) -> bool`：465→SMTPS、其余→SMTP（保持现状语义），10s 超时，异常返回 False 并记 warning。
- 新增 `send_code_email(email: str, code: str, purpose: str) -> bool`：
  - `email_enabled=False` → 记 info 返回 False（测试/本地由 mock 或开关处理）
  - 中文文案：6 位码、10 分钟有效期、"若非本人操作请忽略"；purpose 决定主题（注册验证/登录验证/邮箱验证）。
- `send_verification_email` 保留（老链接流），内部改走 `_send_smtp`，行为不变（仍不抛错——它服务的场景不再是关键入口）。

## 6. 端点契约

全部遵守错误契约 `{"error": {"code", "message}}` + `X-Gapi-Error: 1`（`GapiError` 既有机制自动满足）。

### 6.1 `POST /auth/register/code/request`

- body：`{email: EmailStr, fingerprint?: str}`
- 限流（顺序执行）：`regcode_ip:{ip}` 10/h；`regcode:{email}` 3/h；`regcode_cd:{email}` 1/60s（重发冷却）；有 fingerprint 时 `regcode_fp:{fp}` 3/h
- `coin.registration_open(db)` False → 403 `registration_closed`
- 邮箱已注册 → 409 `email_taken`（不发码；与 register 端点既有 409 语义一致，非新增枚举面）
- 发码 → `send_code_email` False → 作废该码 → 503 `email_send_failed`（"验证邮件发送失败，请稍后再试"，不暴露 SMTP 细节）
- 成功：`{detail: "验证码已发送，请查收", expires_in: 600}`

### 6.2 `POST /auth/register`（改造）

- `RegisterRequest` 增加 `code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")`
- 保留现有限流（`register:{ip}` 3/h、`register_fp:{fp}` 3/h）与 BEGIN IMMEDIATE、首用户 admin 纪律
- 流程内插入原子验码（purpose="register"）：失败 → 400 `invalid_code`（"验证码错误或已过期"）
- 建号：`email_verified=True`；注册奖励**当场入账**（复用现 `verify-email` 内的 capped 发放逻辑：bonus>0 时 `min(old+bonus, max_coin_per_user)` + `CreditTransaction(tx_type="signup_bonus")`）
- 不再创建 `EmailVerification` 行、不再发链接邮件
- 指纹纪律不变（`fingerprint_in_use` 409）
- 响应不变：`TokenResponse` + cookie，201

### 6.3 `POST /auth/login/code/request`

- body：`{email: EmailStr}`
- 限流：`logcode_ip:{ip}` 10/h；`logcode:{email}` 3/h；`logcode_cd:{email}` 1/60s
- 邮箱未注册 → 404 `account_not_found`（"该邮箱尚未注册"，决策点 A；前端据此引导注册 tab）
- 发码/发信失败处理同 6.1
- 成功：`{detail, expires_in: 600}`

### 6.4 `POST /auth/login/code/verify`

- body：`{email: EmailStr, code: str(6 位), fingerprint?: str}`，响应 `TokenResponse`
- 限流：`codeverify:{ip}` 20/h（防扫）
- 原子验码（purpose="login"）失败 → 401 `invalid_code`（统一文案，不区分无码/错码/过期/锁码）
- 用户不存在 → 401 `invalid_code`（竞态：发码后账号被删；不泄露）
- `is_active` False → 403 `account_inactive`（与密码登录一致）
- `email_verified` False → 置 True（凭码补验）
- 指纹：镜像密码登录——`_register_fingerprint`（含 409 纪律）→ 有变更先 commit → `coin.award_daily_bonus(db, user)` 每日签到
- 成功：`_issue(user)` + `_set_session`

### 6.5 `POST /auth/resend-verification`（改造）

- JWT 必需；已验证 → `{detail: "邮箱已验证，无需重发"}`（不变）
- 限流保留 `resend:{user.id}` 3/h，加 `resend_cd:{user.id}` 1/60s
- 改发 purpose="verify" 验证码（不再删老 `EmailVerification` 行——在途链接保持可验证）
- 发信失败 → 503 `email_send_failed`

### 6.6 `POST /auth/verify-email-code`（新增）

- JWT 必需；body `{code: str(6 位)}`
- 已验证 → `{detail: "邮箱已验证"}` 直接返回
- 原子验码（purpose="verify"，email=当前用户邮箱）失败 → 400 `invalid_code`
- 成功 → `email_verified=True` → `{detail: "邮箱已验证"}`
- **不发注册奖励**（老账号不在注册流程内；奖励只发生在 6.2 建号时）

### 6.7 不动的端点

`POST /auth/login`、`GET /auth/verify-email`、`POST /auth/logout`、`GET /auth/me`。

## 7. 安全设计

| 威胁 | 对策 |
|---|---|
| 码爆破（10^6 空间） | 单码 5 次尝试锁 + `codeverify:{ip}` 20/h + 10 分钟 TTL。期望爆破成本 ≫ TTL |
| 并发复用同一码 | `BEGIN IMMEDIATE` 原子消费（对标 `test_concurrent_reserves_cannot_overdraw` 纪律） |
| 码泄露（库拖库） | SHA256 哈希存储，明文只在邮件 |
| 发码轰炸（短信式骚扰） | 每邮箱 3/h + 60s 冷却 + 每 IP 10/h + 指纹 3/h |
| 注册状态枚举 | 6.1 的 409 与既有 register 409 同语义；6.4 统一 401 文案不区分失败原因 |
| SMTP 凭据 | 只存 `.env`（已 untrack）；代码内零硬编码 |
| 指纹滥用 | 与密码登录同一 `_register_fingerprint` 纪律（跨账号 409） |
| 发信静默失败 | 端点显式 503，用户可感知、可重试 |

## 8. 前端改动（web/public，零构建）

`index.html` + `app.js`（CSP 无 inline，全走外部 JS；`?v=` 版本串递增）：

- **登录卡片改双 tab**：「密码登录」（现状表单）/「验证码登录」（邮箱 + [发送验证码] + 6 位码输入 + [登录]）。
- **注册态**：在邮箱、密码之间插入「邮箱验证码」字段 + [发送验证码] 按钮。
- [发送验证码] 点击后 60s 倒计时禁用；收到 404 `account_not_found` 时提示"该邮箱尚未注册，已为你切换到注册"。
- 指纹：所有新端点请求带 `X-Fingerprint`（沿用 fp.js/`ctx.getFingerprint()` 纪律）。
- 面板内「重发验证」流程改为输码（`panel.js` 对应区域 + `POST /auth/verify-email-code`）。

## 9. 迁移与兼容

- alembic：`e5f6a7b8c9d0_add_email_codes.py`，`down_revision='d4e5f6a7b8c9'`，建表 + 复合索引。
- `EmailVerification` 表与 `GET /auth/verify-email` 保留（在途链接 24h 内可验证）。
- 历史未验证账号：密码登录放行（决策 B）；三条补验路径——面板收码、验证码登录顺带置 True、老链接点击。
- `.env.example` 无需新增变量（SMTP 配置复用）；文档注明 163 邮箱要点（见 §11）。

## 10. 测试计划

新文件 `tests/test_phase14_email_code.py`（阶段命名纪律；conftest 已前置 `GAPI_EMAIL_ENABLED=false`，测试 patch `app.services.email._send_smtp` 或 `send_code_email` 捕获明文码）：

1. 注册全流程：request（mock 捕获码）→ register 带码 → 201 + 奖励当场到账 + `email_verified=True` + 不再创建 EmailVerification 行
2. 注册错码 → 400 `invalid_code`；连错 5 次 → 码锁（第 6 次同码也 400）；过期码 → 400
3. 重复 request 作废旧码：旧码 register 400、新码成功
4. purpose 隔离：register 码不能 login verify，login 码不能 register
5. 登录码全流程：request → verify → JWT + cookie + `email_verified` 置 True（先造未验证老账号）
6. 登录码遇未注册邮箱 → 404 `account_not_found`
7. **并发：同一码两个并发 verify 只有一个成功**（线程池打 ASGI，对标 phase3 并发测试写法）
8. 发信失败（mock False）→ 503 且库中无活码
9. 限流：60s 冷却 429、每邮箱 3/h 429、codeverify 20/h 429
10. `resend-verification` 改发码 + `verify-email-code` 补验成功；老链接 `verify-email` 回归仍可用
11. 验证码登录的指纹登记：新指纹自动绑定；跨账号指纹 409
12. 禁用账号验证码登录 → 403 `account_inactive`

## 11. 运维注意（163 邮箱）

- `SMTP_PASSWORD` 必须是 163 的**客户端授权码**，不是登录密码。
- `EMAIL_SENDER` 必须与 `SMTP_USER` 一致（163 强制 From=账号，否则 553 拒发）。
- 免费 163 有日发送上限（数百封/天量级）与频率风控；发码量受 §7 限流矩阵天然约束。
- `.env` 改动需重启进程（uvicorn --reload 不监控它，CLAUDE.md 既有结论）。

## 12. 非目标（YAGNI）

- 不做密码登录的强制验证门槛（决策 B 已选放行）。
- 不做验证码自动注册（决策 A）。
- 不引入第三方邮件 SDK（SendGrid/Resend 等）——smtplib 单向外发已满足需求；`_send_smtp` 抽层后未来换 provider 只动一处。
- 不改 `EmailVerification` 老表结构、不做数据回填。
- 不做多实例共享限流（单实例内存限流是项目既有约束）。
