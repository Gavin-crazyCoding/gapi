# 邮箱验证码认证实施计划（注册收码 + 登录双轨）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 注册必须邮箱验证码（当场验证当场发奖励），登录双轨（密码 / 验证码免密），邮件单向纯外发且失败对用户可见。

**Architecture:** 新 `email_codes` 表（SHA256 哈希存码、purpose 隔离、原子消费）+ 新服务 `app/services/emailcode.py` + `app/services/email.py` 改造（`_send_smtp` 返回 bool，失败不再静默）+ auth 路由新增 4 端点、改造 2 端点 + 公共登录页双 tab 前端。

**Tech Stack:** FastAPI / SQLAlchemy 2.0 同步 / SQLite（WAL, BEGIN IMMEDIATE）/ smtplib（单向）/ pydantic v2 / pytest + httpx ASGI / 零构建原生 JS。

**Spec:** `docs/superpowers/specs/2026-09-25-email-code-auth-design.md`（已获用户批准；本计划逐条实现之）

## Global Constraints

- 测试命令必须带 PYTHONPATH：`PYTHONPATH=$(pwd) .venv/bin/pytest -q`；pytest-asyncio 为 auto 模式，测试函数直接 `async def`。
- conftest 已在 import app 前设好 `GAPI_DATABASE_URL`（临时库）、`GAPI_JWT_SECRET`、`GAPI_EMAIL_ENABLED=false`、`GAPI_REGISTRATION_BONUS=20`——**测试里发邮件一律 mock `app.services.email.send_code_email`，绝不碰 SMTP**。
- 错误契约不可破坏：gapi 自身错误 `{"error":{"code","message"}}` + `X-Gapi-Error: 1` 头，统一抛 `GapiError(status, code, message)`（`app/errors.py`）。
- `email_codes` 时间列全链路 **naive UTC**（`datetime.now(timezone.utc).replace(tzinfo=None)`）；与 aware datetime 混比在 SQLite 上即 TypeError。
- 码明文只出现在邮件里，库中只存 SHA256 hex（与 gapi Key 同纪律）。
- 事务纪律：`issue_code`/`invalidate_codes` 自管理 `BEGIN IMMEDIATE…commit`，**不得在已开事务中调用**；`consume_code` 在调用方事务内运行，失败路径自 commit、成功路径只 flush（详见 Task 2 注释）。
- 指纹纪律：登录/注册属"有背书"场景，新指纹自动登记；跨账号指纹 409 `fingerprint_in_use`（复用 `auth.py::_register_fingerprint`）。
- 签到纪律：指纹绑定必须先于 `coin.award_daily_bonus` commit（其内部 BEGIN IMMEDIATE 会隐式回滚未提交写入）。
- 前端：CSP 无 inline script，全部逻辑在外部 JS；新增颜色必须走既有 CSS 变量；变更资产 `?v=` 版本串递增到 `20260925a`。
- 缓存纪律：测试无需手动清 settings/pricing/ratelimit 缓存（conftest autouse 已处理）。
- 每个 Task 结束跑全套测试保持绿色再 commit；commit message 结尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

（spec 暗示但单任务测试未必覆盖、最可能咬人的五类输入/失败模式；每条的钉住测试已编入所属 Task）

1. **naive/aware 时间混比**：`consume_code` 拿 aware now 比 naive 列 → TypeError 500。→ Task 2 服务测试 + Task 5/6 端点全流程测试钉住。
2. **隐藏但 `required` 的表单字段阻塞提交**：登录切到验证码 tab 后 password 仍 required → 浏览器静默拒绝提交。→ Task 8 用 `disabled` 切换（disabled 输入不参与校验）+ 冒烟步骤三种形态各提交一次。
3. **拒绝顺序烧码**：若 `email_taken`/`registration_closed` 检查在消费码之后，探测请求会烧掉用户合法的码。→ Task 5 测试断言：403/409 后旧码仍可成功注册。
4. **attempts 递增被错误回滚冲掉**：错码递增若只 flush，get_db 关闭回滚 → 锁码失效。→ Task 2 测试：跨"请求"（跨 session）连续错 5 次后第 6 次即使正确也拒绝。
5. **signup_bonus 二次入账**：已验证用户点击在途老链接，`verify-email` 老逻辑不得再发一次奖励。→ Task 7 回归测试断言余额不变、无新 `signup_bonus` 流水。

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `app/models/email_code.py` | `EmailCode` ORM 模型（naive UTC） | 新建 (T1) |
| `app/models/__init__.py` | 导出 `EmailCode` | 改 (T1) |
| `alembic/versions/e5f6a7b8c9d0_add_email_codes.py` | 建表迁移，down_revision=`d4e5f6a7b8c9` | 新建 (T1) |
| `app/services/emailcode.py` | 码的生成/签发/消费/作废；事务纪律单点 | 新建 (T2) |
| `app/services/email.py` | 单向 SMTP；`_send_smtp`→bool；`send_code_email` | 重写 (T3/T7) |
| `app/schemas/auth.py` | `RegisterRequest+code`；`RegisterCodeRequest`/`LoginCodeRequest`/`CodeLoginVerify`/`VerifyEmailCodeIn` | 改 (T5/T6/T7) |
| `app/routers/auth.py` | 4 新端点 + register/resend 改造 | 改 (T5/T6/T7) |
| `tests/conftest.py` | `issue_code_for`/`register_user`/`set_email_verified` + `registered_admin` 改造 | 改 (T4) |
| 9 个存量测试文件 | 23 处 `/auth/register` 调用迁移 | 改 (T5/T7) |
| `tests/test_phase14_email_code.py` | 全部新测试（服务级+端点级） | 新建 (T1/T2/T3/T5/T6/T7) |
| `web/public/index.html` / `app.js` / `style.css` | 登录双 tab + 注册码字段 + 60s 倒计时 | 改 (T8) |
| `docs/design.md` / `README.md` / `CLAUDE.md` / `.env.example` | 文档同步 | 改 (T9) |

---

### Task 1: EmailCode 模型 + alembic 迁移

**Files:**
- Create: `app/models/email_code.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/e5f6a7b8c9d0_add_email_codes.py`
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: `app.database.Base`
- Produces: `EmailCode`（字段：`id/email/purpose/code_hash/expires_at/attempts/consumed_at/created_ip/created_at`，全 naive UTC）；复合索引 `ix_email_codes_email_purpose(email, purpose)`。Task 2 的 `emailcode` 服务只依赖这个模型。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_phase14_email_code.py`）

```python
"""Phase 14: email verification codes (register-with-code + dual-track login)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import EmailCode, EmailVerification, User, CreditTransaction


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def test_email_code_model_roundtrip(client):
    """Rows persist with naive UTC datetimes; live-code filtering works."""
    db = SessionLocal()
    try:
        row = EmailCode(
            email="m@x.com",
            purpose="register",
            code_hash="ab" * 32,
            expires_at=_naive_now() + timedelta(minutes=10),
            created_ip="127.0.0.1",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.id is not None
        assert row.attempts == 0
        assert row.consumed_at is None
        assert row.created_at is not None
        assert row.created_at.tzinfo is None  # naive UTC discipline

        live = db.scalar(
            select(EmailCode).where(
                EmailCode.email == "m@x.com",
                EmailCode.consumed_at.is_(None),
                EmailCode.expires_at > _naive_now(),
            )
        )
        assert live is not None and live.id == row.id
    finally:
        db.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q`
Expected: FAIL，`ImportError: cannot import name 'EmailCode' from 'app.models'`

- [ ] **Step 3: 实现模型 + 导出 + 迁移**

新建 `app/models/email_code.py`：

```python
"""Email verification code model (register / login / verify purposes)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    # Naive UTC: mixing aware/naive in SQLite comparisons raises TypeError.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmailCode(Base):
    __tablename__ = "email_codes"
    __table_args__ = (Index("ix_email_codes_email_purpose", "email", "purpose"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # register|login|verify
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA256 hex
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
```

`app/models/__init__.py`：加 `from app.models.email_code import EmailCode`（放在 `EmailVerification` import 之后），`__all__` 列表加 `"EmailCode"`（放在 `"CreditTransaction"` 之后，保持字母序）。

新建 `alembic/versions/e5f6a7b8c9d0_add_email_codes.py`：

```python
"""add email_codes table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'email_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('created_ip', sa.String(length=45), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_email_codes_email_purpose', 'email_codes', ['email', 'purpose'])


def downgrade() -> None:
    op.drop_index('ix_email_codes_email_purpose', table_name='email_codes')
    op.drop_table('email_codes')
```

- [ ] **Step 4: 跑测试确认通过 + 迁移冒烟**

```bash
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q   # PASS
rm -f /tmp/gapi-alembic-check.db
GAPI_DATABASE_URL=sqlite:////tmp/gapi-alembic-check.db .venv/bin/alembic upgrade head   # 链到 e5f6a7b8c9d0
GAPI_DATABASE_URL=sqlite:////tmp/gapi-alembic-check.db .venv/bin/alembic downgrade -1   # 回退正常
GAPI_DATABASE_URL=sqlite:////tmp/gapi-alembic-check.db .venv/bin/alembic upgrade head   # 再升级
rm -f /tmp/gapi-alembic-check.db
```
Expected: 测试 PASS；alembic 链无断点（down_revision 对上 `d4e5f6a7b8c9`）。

- [ ] **Step 5: Commit**

```bash
git add app/models/email_code.py app/models/__init__.py alembic/versions/e5f6a7b8c9d0_add_email_codes.py tests/test_phase14_email_code.py
git commit -m "feat: EmailCode model + alembic migration

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: emailcode 服务（生成/签发/消费/作废）

**Files:**
- Create: `app/services/emailcode.py`
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: Task 1 的 `EmailCode`；`app.errors.GapiError`
- Produces（Task 4/5/6/7 全部依赖，签名一字不改）：
  - `CODE_TTL_SECONDS: int = 600`、`MAX_ATTEMPTS: int = 5`
  - `generate_code() -> str`
  - `issue_code(db: Session, *, email: str, purpose: str, ip: str | None = None) -> str`（自事务）
  - `invalidate_codes(db: Session, *, email: str, purpose: str) -> int`（自事务）
  - `consume_code(db: Session, *, email: str, purpose: str, code: str, error_status: int = 400) -> None`（调用方事务内；失败抛 `GapiError(error_status, "invalid_code", "验证码错误或已过期")`）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_phase14_email_code.py`）

```python
# ── emailcode service ─────────────────────────────────────────────

def _issue(email: str, purpose: str = "register") -> str:
    from app.services import emailcode
    db = SessionLocal()
    try:
        return emailcode.issue_code(db, email=email, purpose=purpose, ip="test")
    finally:
        db.close()


def _consume(email: str, purpose: str, code: str, error_status: int = 400) -> None:
    from app.services import emailcode
    db = SessionLocal()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        emailcode.consume_code(db, email=email, purpose=purpose, code=code, error_status=error_status)
        db.commit()
    finally:
        db.close()


async def test_generate_code_format(client):
    from app.services import emailcode
    codes = {emailcode.generate_code() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 150  # CSPRNG spread


async def test_issue_invalidates_prior_live_codes(client):
    from app.services import emailcode
    first = _issue("s@x.com")
    second = _issue("s@x.com")
    assert first != second
    db = SessionLocal()
    try:
        live = db.scalars(
            select(EmailCode).where(EmailCode.email == "s@x.com", EmailCode.consumed_at.is_(None))
        ).all()
        assert len(live) == 1  # 旧码已作废
        # 库中只存哈希
        assert all(row.code_hash != second for row in db.scalars(select(EmailCode)).all())
        assert len(live[0].code_hash) == 64
    finally:
        db.close()


async def test_consume_success_then_replay_fails(client):
    code = _issue("c@x.com")
    _consume("c@x.com", "register", code)  # 第一次成功
    with pytest.raises(Exception) as ei:
        _consume("c@x.com", "register", code)  # 重放失败
    assert "invalid_code" in str(ei.value)


async def test_consume_wrong_code_increments_and_locks_at_five(client):
    from app.services import emailcode
    code = _issue("w@x.com")
    wrong = "000000" if code != "000000" else "000001"
    for _ in range(emailcode.MAX_ATTEMPTS):
        with pytest.raises(Exception):
            _consume("w@x.com", "register", wrong)
    # 锁码后，即使正确码也被拒（attempts 跨 session 存活 = 失败路径自 commit 生效）
    with pytest.raises(Exception):
        _consume("w@x.com", "register", code)
    db = SessionLocal()
    try:
        row = db.scalar(select(EmailCode).where(EmailCode.email == "w@x.com"))
        assert row.attempts == emailcode.MAX_ATTEMPTS
        assert row.consumed_at is not None  # 锁码即作废
    finally:
        db.close()


async def test_consume_expired_code_rejected(client):
    from app.services import emailcode
    code = _issue("e@x.com")
    db = SessionLocal()
    try:
        row = db.scalar(select(EmailCode).where(EmailCode.email == "e@x.com"))
        row.expires_at = _naive_now() - timedelta(seconds=1)  # 手动置过期
        db.commit()
    finally:
        db.close()
    with pytest.raises(Exception):
        _consume("e@x.com", "register", code)


async def test_consume_purpose_isolated(client):
    code = _issue("p@x.com", purpose="register")
    with pytest.raises(Exception):
        _consume("p@x.com", "login", code)  # register 码不能当 login 码用


async def test_concurrent_consume_only_one_wins(client):
    """镜像 phase3 并发纪律：BEGIN IMMEDIATE 序列化，同码并发消费只有一个成功。"""
    code = _issue("race@x.com")
    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(tag: str):
        from app.services import emailcode
        db = SessionLocal()
        try:
            db.execute(text("BEGIN IMMEDIATE"))
            barrier.wait(timeout=5)
            emailcode.consume_code(db, email="race@x.com", purpose="register", code=code)
            db.commit()
            results.append(f"{tag}:ok")
        except Exception:
            db.rollback()
            results.append(f"{tag}:fail")
        finally:
            db.close()

    t1 = threading.Thread(target=attempt, args=("a",))
    t2 = threading.Thread(target=attempt, args=("b",))
    t1.start(); t2.start(); t1.join(timeout=15); t2.join(timeout=15)
    assert sorted(results) == ["a:ok", "b:fail"] or sorted(results) == ["a:fail", "b:ok"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k "generate or issue or consume"`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.emailcode'`

- [ ] **Step 3: 实现 `app/services/emailcode.py`**

```python
"""Email verification codes: issue / consume / invalidate.

事务纪律（单点维护，调用方必须遵守）：
- issue_code / invalidate_codes 自管理 BEGIN IMMEDIATE…commit，
  不得在已开事务中调用（SQLite 不支持嵌套 BEGIN）。
- consume_code 在【调用方】的 BEGIN IMMEDIATE 内运行：
  · 失败路径（错码/锁码）自 commit——attempts 递增与锁码必须挺过
    请求出错时的会话回滚，否则 5 次锁码形同虚设；
  · 成功路径只 flush——消费与调用方的业务写入同生共死
    （如注册撞上 email 唯一索引回滚时，码不被白白烧掉）。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.errors import GapiError
from app.models import EmailCode

CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5
PURPOSES = {"register", "login", "verify"}


def _utcnow() -> datetime:
    # Naive UTC：email_codes 全列 naive，比较时绝不引入 aware（SQLite TypeError）。
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def generate_code() -> str:
    """6 位数字码，CSPRNG。"""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _invalidate(db: Session, *, email: str, purpose: str) -> int:
    """作废同 email+purpose 的全部未消费码（不写库提交，由外层事务负责）。"""
    result = db.execute(
        update(EmailCode)
        .where(
            EmailCode.email == email,
            EmailCode.purpose == purpose,
            EmailCode.consumed_at.is_(None),
        )
        .values(consumed_at=_utcnow())
    )
    return result.rowcount or 0


def issue_code(db: Session, *, email: str, purpose: str, ip: str | None = None) -> str:
    """作废旧码 → 插入新码（哈希）→ 返回明文码。自事务，勿在事务内调用。"""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose: {purpose}")
    db.execute(text("BEGIN IMMEDIATE"))
    _invalidate(db, email=email, purpose=purpose)
    code = generate_code()
    db.add(
        EmailCode(
            email=email,
            purpose=purpose,
            code_hash=_hash(code),
            expires_at=_utcnow() + timedelta(seconds=CODE_TTL_SECONDS),
            created_ip=ip,
        )
    )
    db.commit()
    return code


def invalidate_codes(db: Session, *, email: str, purpose: str) -> int:
    """公开作废入口（发信失败回滚用）。自事务，勿在事务内调用。"""
    db.execute(text("BEGIN IMMEDIATE"))
    n = _invalidate(db, email=email, purpose=purpose)
    db.commit()
    return n


def consume_code(
    db: Session, *, email: str, purpose: str, code: str, error_status: int = 400
) -> None:
    """原子验证+消费最新活码。失败抛 GapiError(error_status, "invalid_code", ...)。"""

    def fail() -> None:
        raise GapiError(error_status, "invalid_code", "验证码错误或已过期")

    ev = db.scalar(
        select(EmailCode)
        .where(
            EmailCode.email == email,
            EmailCode.purpose == purpose,
            EmailCode.consumed_at.is_(None),
        )
        .order_by(EmailCode.id.desc())
        .limit(1)
    )
    if ev is None or ev.expires_at <= _utcnow():
        fail()
    if ev.attempts >= MAX_ATTEMPTS:
        ev.consumed_at = _utcnow()  # 锁码即作废
        db.commit()  # 失败路径自 commit：锁码必须落库
        fail()
    if ev.code_hash != _hash(code):
        ev.attempts += 1
        db.commit()  # 失败路径自 commit：递增必须落库
        fail()
    ev.consumed_at = _utcnow()
    db.flush()  # 成功路径：随调用方业务一起 commit
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q`
Expected: 全 PASS（含并发用例）

- [ ] **Step 5: Commit**

```bash
git add app/services/emailcode.py tests/test_phase14_email_code.py
git commit -m "feat: emailcode service (issue/consume/invalidate, atomic single-use)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: email.py 改造（`_send_smtp`→bool + `send_code_email`）

**Files:**
- Modify: `app/services/email.py`（重写）
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: `app.config.settings`（`email_enabled/smtp_host/smtp_port/smtp_user/smtp_password/email_sender`）
- Produces：
  - `_send_smtp(msg: EmailMessage) -> bool`（内部；测试可 patch）
  - `send_code_email(email: str, code: str, purpose: str) -> bool`——Task 5/6/7 的端点通过 `from app.services import email as mailer` 调 `mailer.send_code_email(...)`（**模块属性调用**，测试 patch `app.services.email.send_code_email` 一处即生效）
  - `send_verification_email(email, token)` 本任务保留（老链接流唯一调用方是 resend，Task 7 删除），行为不变（永不抛异常），内部改走 `_send_smtp`

- [ ] **Step 1: 写失败测试**（追加）

```python
# ── email service ─────────────────────────────────────────────────

class _FakeSMTP:
    """捕获型 SMTP 假身：记录 send_message，按类属性决定抛错与否。"""

    instances: list = []
    fail_on_send: bool = False

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        if _FakeSMTP.fail_on_send:
            import smtplib
            raise smtplib.SMTPException("boom")
        self.sent.append(msg)


@pytest.fixture()
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    _FakeSMTP.fail_on_send = False
    monkeypatch.setattr("app.services.email.smtplib.SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr("app.services.email.smtplib.SMTP", _FakeSMTP)
    return _FakeSMTP


def _mail_settings(monkeypatch, *, enabled=True, port=465):
    from app.config import settings
    monkeypatch.setattr(settings, "email_enabled", enabled)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_port", port)
    monkeypatch.setattr(settings, "smtp_user", "u@test")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "email_sender", "u@test")


async def test_send_code_email_disabled_returns_false(client, monkeypatch):
    from app.services import email as mailer
    _mail_settings(monkeypatch, enabled=False)
    assert mailer.send_code_email("a@x.com", "123456", "login") is False


async def test_send_code_email_ssl_port_content(client, fake_smtp, monkeypatch):
    from app.services import email as mailer
    _mail_settings(monkeypatch, port=465)
    assert mailer.send_code_email("a@x.com", "654321", "register") is True
    inst = fake_smtp.instances[0]
    assert inst.port == 465
    assert inst.logged_in == ("u@test", "secret")
    msg = inst.sent[0]
    assert msg["To"] == "a@x.com"
    assert msg["From"] == "u@test"
    assert "注册" in msg["Subject"]
    assert "654321" in msg.get_content()


async def test_send_code_email_plain_port_and_failure(client, fake_smtp, monkeypatch):
    from app.services import email as mailer
    _mail_settings(monkeypatch, port=25)
    assert mailer.send_code_email("a@x.com", "111111", "login") is True
    assert fake_smtp.instances[0].port == 25
    assert "登录" in fake_smtp.instances[0].sent[0]["Subject"]
    fake_smtp.fail_on_send = True
    assert mailer.send_code_email("a@x.com", "111111", "login") is False  # 不抛异常
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k send_code_email`
Expected: FAIL，`AttributeError: module 'app.services.email' has no attribute 'send_code_email'`

- [ ] **Step 3: 重写 `app/services/email.py`**

```python
"""Outbound-only email sending for gapi (verification codes).

纯外发：smtplib 单向投递，不涉及任何收信基础设施。
_send_smtp 返回 bool——验证码是注册/登录的唯一入口，发送失败必须可感知，
由调用方决定如何呈现（端点映射为 503 email_send_failed）。
"""

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("gapi.email")

# 465 = SMTPS（首字节即 TLS）；其余端口走明文/STARTTLS 语义的 plain SMTP。
_SSL_PORTS = {465}
_CONNECT_TIMEOUT = 10.0

_SUBJECTS = {
    "register": "gapi 注册验证码",
    "login": "gapi 登录验证码",
    "verify": "gapi 邮箱验证码",
}


def _send_smtp(msg: EmailMessage) -> bool:
    """投递一封邮件；任何失败（连接/认证/协议）返回 False 并记 warning。"""
    try:
        if settings.smtp_port in _SSL_PORTS:
            smtp_cm = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=_CONNECT_TIMEOUT
            )
        else:
            smtp_cm = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=_CONNECT_TIMEOUT
            )
        with smtp_cm as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("SMTP delivery failed (email not sent): %s", exc)
        return False


def send_code_email(email: str, code: str, purpose: str) -> bool:
    """发送 6 位验证码邮件。disabled 或投递失败都返回 False。"""
    if not settings.email_enabled:
        log.info("email disabled; %s code for %s not sent", purpose, email)
        return False
    msg = EmailMessage()
    msg["Subject"] = _SUBJECTS.get(purpose, _SUBJECTS["verify"])
    msg["From"] = settings.email_sender
    msg["To"] = email
    msg.set_content(
        f"您的验证码是：{code}（10 分钟内有效）。\n如果不是您本人操作，请忽略此邮件。",
        subtype="plain",
    )
    return _send_smtp(msg)


def send_verification_email(email: str, token: str) -> bool:
    """【遗留】链接式验证邮件，仅供在途老链接流程（Task 7 移除调用方后删除）。

    与历史行为一致：失败只记日志返回 False，不抛异常。
    """
    if not settings.email_enabled:
        log.info("email disabled; verification link for %s not sent", email)
        return False
    msg = EmailMessage()
    msg["Subject"] = "请验证您的 gapi 账号"
    msg["From"] = settings.email_sender
    msg["To"] = email
    link = f"{settings.base_url}/auth/verify-email?token={token}"
    msg.set_content(
        f"点击下面的链接完成邮箱验证（链接 24 小时内有效）：\n{link}\n如果不是您本人操作，请忽略此邮件。",
        subtype="plain",
    )
    return _send_smtp(msg)
```

注意：`app/routers/auth.py` 此刻仍 `from app.services.email import send_verification_email`——签名保留，本任务不动 auth.py。

- [ ] **Step 4: 跑测试（本文件 + 全套）**

```bash
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q
PYTHONPATH=$(pwd) .venv/bin/pytest -q
```
Expected: 全 PASS（无行为回归）

- [ ] **Step 5: Commit**

```bash
git add app/services/email.py tests/test_phase14_email_code.py
git commit -m "refactor: email service returns delivery status, add send_code_email

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: conftest 测试基建（ Helpers + registered_admin 改造）

**Files:**
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: Task 2 的 `emailcode.issue_code`
- Produces（后续所有测试任务依赖）：
  - `issue_code_for(email: str, purpose: str = "register") -> str`（同步，服务级直插，无 SMTP）
  - `register_user(client, email, password="hunter2hunter", fingerprint=None) -> httpx.Response`（断言 201）
  - `set_email_verified(email: str, verified: bool) -> None`（构造遗留未验证账号）
  - `registered_admin` fixture 行为不变（返回 `{"email","password",**token_json}`）

- [ ] **Step 1: 改造 conftest**

`tests/conftest.py` 顶部 import 块（`from app.database import Base, engine` 那行）改为：

```python
from app.database import Base, SessionLocal, engine  # noqa: E402
```

文件末尾追加：

```python
def issue_code_for(email: str, purpose: str = "register") -> str:
    """服务级直插一个活码（绕过 SMTP），返回明文码。"""
    from app.services import emailcode

    db = SessionLocal()
    try:
        return emailcode.issue_code(db, email=email.lower().strip(), purpose=purpose, ip="test")
    finally:
        db.close()


async def register_user(client, email: str, password: str = "hunter2hunter", fingerprint: str | None = None):
    """走完整注册流程（先直插码再 POST /auth/register），断言 201。

    注意：Task 5 之前旧 RegisterRequest 尚无 code 字段——pydantic v2 默认忽略
    多余字段，此 helper 提前带上 code 也不会破坏旧端点。
    """
    payload: dict = {"email": email, "password": password, "code": issue_code_for(email)}
    if fingerprint:
        payload["fingerprint"] = fingerprint
    resp = await client.post("/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    return resp


def set_email_verified(email: str, verified: bool) -> None:
    """构造遗留未验证账号（新注册流 100% 已验证，只能直改库）。"""
    from sqlalchemy import text as _text

    db = SessionLocal()
    try:
        db.execute(
            _text("UPDATE users SET email_verified=:v WHERE email=:e"),
            {"v": 1 if verified else 0, "e": email.lower().strip()},
        )
        db.commit()
    finally:
        db.close()
```

`registered_admin` fixture 改为：

```python
@pytest.fixture()
async def registered_admin(client):
    resp = await register_user(client, "admin@example.com")
    data = resp.json()
    return {"email": "admin@example.com", "password": "hunter2hunter", **data}
```

- [ ] **Step 2: 全套测试确认绿色**（helper 先行、旧端点忽略多余 code 字段）

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest -q`
Expected: 107 全 PASS（如有测试恰好断言了 register 请求体验证细节，逐个看——预期无）

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: conftest helpers for code-based registration flow

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 注册收码（端点实现 + 存量测试迁移）

**Files:**
- Modify: `app/schemas/auth.py`（RegisterRequest + code；新增 RegisterCodeRequest）
- Modify: `app/routers/auth.py`（新增 `/auth/register/code/request`；重写 `/auth/register`）
- Modify: 9 个存量测试文件（23 处 register 调用 + 语义变化断言）
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: Task 2 `emailcode`、Task 3 `mailer.send_code_email`、Task 4 helpers
- Produces：
  - `POST /auth/register/code/request`：body `{email, fingerprint?}` → 200 `{detail, expires_in:600}`；错误 403 `registration_closed` / 409 `email_taken` / 429 `rate_limited` / 503 `email_send_failed`
  - `POST /auth/register`：body 新增必填 `code`（6 位数字）；400 `invalid_code`；**拒绝顺序**：限流 → `registration_open` → `email_taken` → `consume_code` → 建号（Review Focus #3）
  - 注册成功即 `email_verified=True`、bonus 当场入账（`tx_type="signup_bonus"`），**不再**创建 `EmailVerification` 行、不再发链接邮件

- [ ] **Step 1: 写失败测试**（追加）

```python
# ── register code flow ────────────────────────────────────────────

@pytest.fixture()
def captured_codes(monkeypatch):
    """拦截 send_code_email，记录 (email, code, purpose)，恒真。"""
    sent: list[tuple[str, str, str]] = []

    def fake(email: str, code: str, purpose: str) -> bool:
        sent.append((email, code, purpose))
        return True

    monkeypatch.setattr("app.services.email.send_code_email", fake)
    return sent


async def test_register_code_request_sends_email(client, captured_codes):
    r = await client.post("/auth/register/code/request", json={"email": "new@x.com"})
    assert r.status_code == 200
    assert r.json()["expires_in"] == 600
    assert captured_codes == [("new@x.com", captured_codes[0][1], "register")]
    assert len(captured_codes[0][1]) == 6


async def test_register_full_flow_grants_bonus_and_verified(client, captured_codes):
    await client.post("/auth/register/code/request", json={"email": "flow@x.com"})
    code = captured_codes[0][1]
    r = await client.post(
        "/auth/register",
        json={"email": "flow@x.com", "password": "password123", "code": code},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email_verified"] is True
    assert body["user"]["gavincoin_balance"] == "20"  # conftest 钉死 GAPI_REGISTRATION_BONUS=20
    db = SessionLocal()
    try:
        tx = db.scalar(select(CreditTransaction).where(CreditTransaction.tx_type == "signup_bonus"))
        assert tx is not None and str(tx.amount) == "20"
        # 新流程不再创建链接式验证行
        assert db.scalar(select(EmailVerification)) is None
    finally:
        db.close()


async def test_register_rejects_bad_or_missing_code(client):
    r = await client.post(
        "/auth/register",
        json={"email": "noc@x.com", "password": "password123", "code": "000000"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_code"
    assert r.headers.get("x-gapi-error") == "1"
    r2 = await client.post(
        "/auth/register", json={"email": "noc@x.com", "password": "password123"}
    )
    assert r2.status_code == 422  # code 现为必填字段


async def test_register_code_request_conflict_and_closed(client, captured_codes, registered_admin):
    # 已注册邮箱 → 409（不发码）
    r = await client.post("/auth/register/code/request", json={"email": "admin@example.com"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"
    assert captured_codes == []
    # 关闭注册 → 403
    jwt = {"Authorization": f"Bearer {registered_admin['access_token']}"}
    await client.put("/admin/settings", json=[{"key": "registration_open", "value": "false"}], headers=jwt)
    r2 = await client.post("/auth/register/code/request", json={"email": "late@x.com"})
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "registration_closed"


async def test_register_closed_or_conflict_does_not_burn_code(client, captured_codes):
    """Review Focus #3：先撞 email_taken 的探测不得烧掉合法用户的码。"""
    await client.post("/auth/register/code/request", json={"email": "keep@x.com"})
    code = captured_codes[0][1]
    # 他人抢先注册同邮箱（用另一个码）
    await client.post("/auth/register/code/request", json={"email": "keep@x.com"})
    code2 = captured_codes[1][1]
    r = await client.post(
        "/auth/register", json={"email": "keep@x.com", "password": "password123", "code": code2}
    )
    assert r.status_code == 201
    # 旧码 code 已被第二次 request 作废 → 重放 400（不是 409：email_taken 先于验码）
    r2 = await client.post(
        "/auth/register", json={"email": "keep@x.com", "password": "password123", "code": code}
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "email_taken"


async def test_register_send_failure_503_and_code_dead(client, monkeypatch):
    monkeypatch.setattr("app.services.email.send_code_email", lambda *a, **k: False)
    r = await client.post("/auth/register/code/request", json={"email": "fail@x.com"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "email_send_failed"
    db = SessionLocal()
    try:
        live = db.scalar(
            select(EmailCode).where(EmailCode.email == "fail@x.com", EmailCode.consumed_at.is_(None))
        )
        assert live is None  # 失败即作废，不留"库里活码+用户没收到"
    finally:
        db.close()


async def test_register_code_rate_limits(client, captured_codes):
    for _ in range(3):
        r = await client.post("/auth/register/code/request", json={"email": "rl@x.com"})
        assert r.status_code == 200
        captured_codes.clear()
    r = await client.post("/auth/register/code/request", json={"email": "rl@x.com"})
    assert r.status_code == 429
    r2 = await client.post("/auth/register/code/request", json={"email": "rl2@x.com"})
    assert r2.status_code == 200  # 换邮箱不受影响（独立桶）


async def test_register_wrong_purpose_code_rejected(client):
    login_code = issue_code_for("wp@x.com", purpose="login")
    r = await client.post(
        "/auth/register", json={"email": "wp@x.com", "password": "password123", "code": login_code}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_code"
```

（文件顶部 import 追加 `from conftest import issue_code_for`——`tests/` 无 `__init__.py`，pytest prepend 模式把 `tests/` 目录放入 sys.path，conftest 作为顶层模块可导入。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k register`
Expected: FAIL（404 on `/auth/register/code/request`；register 400/422 断言倒挂）

- [ ] **Step 3: 实现 schemas + 端点**

`app/schemas/auth.py`：`RegisterRequest` 加字段，文件加新 schema：

```python
CODE_FIELD = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RegisterCodeRequest(BaseModel):
    email: EmailStr
    fingerprint: str | None = None


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    code: str = CODE_FIELD  # 6 位邮箱验证码（/auth/register/code/request 签发）
    fingerprint: str | None = None
```

`app/routers/auth.py`：

import 区追加：

```python
from app.services import email as mailer
from app.services import emailcode
from app.schemas.auth import RegisterCodeRequest  # 并入既有 schemas import 行亦可
```

`@router.post("/register", ...)` 之前插入新端点：

```python
@router.post("/register/code/request")
def request_register_code(
    body: RegisterCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.services import coin, ratelimit
    ip = _client_ip(request)
    ratelimit.check(f"regcode_ip:{ip}", limit=10, window_seconds=3600)
    email = body.email.lower().strip()
    ratelimit.check(f"regcode:{email}", limit=3, window_seconds=3600)
    ratelimit.check(f"regcode_cd:{email}", limit=1, window_seconds=60)
    if body.fingerprint:
        ratelimit.check(f"regcode_fp:{body.fingerprint}", limit=3, window_seconds=3600)
    if not coin.registration_open(db):
        raise GapiError(403, "registration_closed", "当前未开放注册")
    if db.scalar(select(User).where(User.email == email)):
        raise GapiError(409, "email_taken", "Email is already registered")
    code = emailcode.issue_code(db, email=email, purpose="register", ip=ip)
    if not mailer.send_code_email(email, code, purpose="register"):
        emailcode.invalidate_codes(db, email=email, purpose="register")
        raise GapiError(503, "email_send_failed", "验证邮件发送失败，请稍后再试")
    return {"detail": "验证码已发送，请查收", "expires_in": emailcode.CODE_TTL_SECONDS}
```

`register` 端点整体替换为：

```python
@router.post("/register", response_model=TokenResponse, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    from app.services import coin, ratelimit
    ratelimit.check(f"register:{_client_ip(request)}", limit=3, window_seconds=3600)
    if body.fingerprint:
        ratelimit.check(
            f"register_fp:{body.fingerprint}", limit=3, window_seconds=3600
        )
    db.execute(text("BEGIN IMMEDIATE"))
    if not coin.registration_open(db):
        raise GapiError(403, "registration_closed", "当前未开放注册")

    email = body.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise GapiError(409, "email_taken", "Email is already registered")

    # 拒绝顺序（Review Focus #3）：registration_closed/email_taken 都在消费码
    # 之前——探测请求不得烧掉合法用户的码。consume 失败路径自 commit
    # （attempts 递增），成功路径随本事务一起提交。
    emailcode.consume_code(db, email=email, purpose="register", code=body.code)

    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    bonus = coin.get_registration_bonus(db)
    # 验证码通过即邮箱所有权已证明：注册奖励当场入账（等价于旧"验证后发放"的
    # 安全保证），开户余额 0 + capped bonus。
    opening = min(bonus, coin.get_max_coin_per_user(db)) if bonus > 0 else Decimal("0")
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        role="admin" if is_first else "user",
        gavincoin_balance=opening,
        email_verified=True,
    )
    db.add(user)
    try:
        db.flush()
        if opening > 0:
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=opening,
                    balance_after=opening,
                    tx_type="signup_bonus",
                    note="registration bonus (email verified)",
                )
            )
        if body.fingerprint:
            _register_fingerprint(db, user, body.fingerprint)
        db.commit()
    except IntegrityError:
        # 并发同邮箱注册：唯一索引裁决；consume 只是 flush，回滚后码仍可用。
        db.rollback()
        raise GapiError(409, "email_taken", "Email is already registered")
    db.refresh(user)
    result = _issue(user)
    _set_session(response, result.access_token, request)
    return result
```

删除 register 里原有的 `EmailVerification` 建行与 `send_verification_email` 调用；`from app.services.email import send_verification_email` import 保留（resend 还在用，Task 7 清理）。

- [ ] **Step 4: 跑新测试确认通过**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k register`
Expected: PASS

- [ ] **Step 5: 迁移存量测试（23 处 + 语义断言）**

迁移规则（对每个 `/auth/register` 调用点逐一应用）：

1. **需要注册成功** → 改用 `await register_user(client, email, password)`（各测试文件 `from conftest import register_user`）；多参数场景（fingerprint）传 `fingerprint=`。
2. **断言预消费拒绝**（409 email_taken / 403 registration_closed）→ 保留直 POST，但 json 必须补 `"code": "000000"`（缺 code 会 422 而非预期状态码）。
3. **断言 422 校验**（弱密码等）→ 不动（缺 code 同样 422）。
4. **并发首 admin 竞态**（`test_phase12_security_fixes.py:155-163`）→ 先 `issue_code_for("race-a@example.com")`、`issue_code_for("race-b@example.com")`，两个 POST 各带自己的码。
5. **大小写/去空格**（`test_phase1_auth.py` 的 `Alice@Example.com  `）→ `issue_code_for("alice@example.com")`（规范化后的邮箱）+ POST 原文邮箱带码。
6. **语义变化断言**：
   - `test_phase7_features.py::test_resend_verification_flow`（:193 起）：`email_verified is False` 断言不再成立。本任务做**过渡改写**：`register_user` 后 `set_email_verified("vrf@x.com", False)`，删掉 `email_verified is False` 断言，后续链接验证步骤保持（resend 链接流 Task 7 才改），断言行改为只断言状态码（此时余额已被注册奖励入账，老 verify-email 会再发一次 bonus——过渡测试**不断言余额**，Task 7 终版改写覆盖）。
   - `test_phase6_audit.py:389/421`（registration_bonus 来自 settings）：读代码确认旧断言路径；新语义下 bonus 在注册当场到账——若旧测试先注册再 verify-email 才断言余额，改为注册后直接断言；若断言注册时余额 0，改为断言 `email_verified is True` + 余额=bonus。
   - 任何其他断言"注册后 `email_verified is False`"或"注册后余额 0"的用例，按新语义改写。

调用点清单（grep 自代码库，执行时以 `grep -rn "auth/register" tests/ | grep -v conftest | grep -v phase14` 复核）：
`test_phase1_auth.py`(5)、`test_phase1_keys.py`(1)、`test_phase5_playground.py`(2)、`test_phase6_audit.py`(1+bonus 测试)、`test_phase7_features.py`(9)、`test_phase8_fingerprint.py`(1)、`test_phase10_panel_nav.py`(1)、`test_phase11_daily_checkin.py`(1)、`test_phase12_security_fixes.py`(2)。

- [ ] **Step 6: 全套测试确认绿色**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest -q`
Expected: 全 PASS（含 phase14 新用例）

- [ ] **Step 7: Commit**

```bash
git add app/schemas/auth.py app/routers/auth.py tests/
git commit -m "feat: registration requires email code, bonus granted at signup

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 验证码登录（免密）

**Files:**
- Modify: `app/schemas/auth.py`（新增 `LoginCodeRequest`/`CodeLoginVerify`）
- Modify: `app/routers/auth.py`（新增 `/auth/login/code/request`、`/auth/login/code/verify`）
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: 同 Task 5；`coin.award_daily_bonus`、`_register_fingerprint`（auth.py 既有）
- Produces：
  - `POST /auth/login/code/request`：body `{email}` → 200 `{detail, expires_in:600}`；404 `account_not_found`（决策 A）；503 `email_send_failed`；429
  - `POST /auth/login/code/verify`：body `{email, code, fingerprint?}` → `TokenResponse`；失败统一 401 `invalid_code`（不区分无码/错码/过期/锁码/账号竞态删除）；`is_active=False` → 403 `account_inactive`；跨账号指纹 409 `fingerprint_in_use`

- [ ] **Step 1: 写失败测试**（追加）

```python
# ── code login flow ───────────────────────────────────────────────

async def _register(client, email: str) -> str:
    """注册并返回明文登录码之外的东西都不需要——只建号。"""
    await register_user(client, email)
    return email


async def test_code_login_full_flow(client, captured_codes):
    await register_user(client, "cl@x.com")
    r = await client.post("/auth/login/code/request", json={"email": "cl@x.com"})
    assert r.status_code == 200
    code = captured_codes[-1][1]
    assert captured_codes[-1][2] == "login"
    r2 = await client.post("/auth/login/code/verify", json={"email": "cl@x.com", "code": code})
    assert r2.status_code == 200, r2.text
    assert r2.json()["access_token"]
    assert r2.json()["user"]["email"] == "cl@x.com"


async def test_code_login_request_unknown_email_404(client):
    r = await client.post("/auth/login/code/request", json={"email": "ghost@x.com"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "account_not_found"
    assert r.headers.get("x-gapi-error") == "1"


async def test_code_login_invalid_code_uniform_401(client, captured_codes):
    await register_user(client, "u401@x.com")
    await client.post("/auth/login/code/request", json={"email": "u401@x.com"})
    good = captured_codes[-1][1]
    bad = "000000" if good != "000000" else "000001"
    r = await client.post("/auth/login/code/verify", json={"email": "u401@x.com", "code": bad})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_code"
    r2 = await client.post("/auth/login/code/verify", json={"email": "u401@x.com", "code": "999999" if good != "999999" else "888888"})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "invalid_code"  # 文案/码不区分失败原因


async def test_code_login_concurrent_same_code_one_wins(client, captured_codes):
    """端点级并发：同一码两个并发 verify 只能成功一次（BEGIN IMMEDIATE 序列化）。"""
    import asyncio

    await register_user(client, "race@x.com")
    await client.post("/auth/login/code/request", json={"email": "race@x.com"})
    code = captured_codes[-1][1]
    r1, r2 = await asyncio.gather(
        client.post("/auth/login/code/verify", json={"email": "race@x.com", "code": code}),
        client.post("/auth/login/code/verify", json={"email": "race@x.com", "code": code}),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 401]


async def test_code_login_backfills_email_verified(client, captured_codes):
    """遗留未验证账号凭码登录即补验（验证码=邮箱所有权证明），不二次发奖励。"""
    await register_user(client, "legacy@x.com")
    set_email_verified("legacy@x.com", False)
    await client.post("/auth/login/code/request", json={"email": "legacy@x.com"})
    code = captured_codes[-1][1]
    r = await client.post("/auth/login/code/verify", json={"email": "legacy@x.com", "code": code})
    assert r.status_code == 200
    assert r.json()["user"]["email_verified"] is True
    db = SessionLocal()
    try:
        txs = db.scalars(
            select(CreditTransaction).where(CreditTransaction.tx_type == "signup_bonus")
        ).all()
        assert len(txs) == 1  # 只有注册那一笔
    finally:
        db.close()


async def test_code_login_inactive_account_403(client, captured_codes):
    await register_user(client, "off@x.com")
    db = SessionLocal()
    try:
        db.execute(text("UPDATE users SET is_active=0 WHERE email='off@x.com'"))
        db.commit()
    finally:
        db.close()
    await client.post("/auth/login/code/request", json={"email": "off@x.com"})
    code = captured_codes[-1][1]
    r = await client.post("/auth/login/code/verify", json={"email": "off@x.com", "code": code})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_inactive"


async def test_code_login_registers_fingerprint(client, captured_codes):
    await register_user(client, "fp@x.com")
    await client.post("/auth/login/code/request", json={"email": "fp@x.com"})
    code = captured_codes[-1][1]
    r = await client.post(
        "/auth/login/code/verify",
        json={"email": "fp@x.com", "code": code, "fingerprint": "fp-new-device"},
    )
    assert r.status_code == 200
    db = SessionLocal()
    try:
        n = db.scalar(text("SELECT count(*) FROM user_fingerprints WHERE fingerprint_hash='fp-new-device'"))
        assert n == 1
    finally:
        db.close()


async def test_code_login_verify_rate_limit(client, captured_codes):
    await register_user(client, "rlv@x.com")
    for _ in range(20):
        await client.post("/auth/login/code/verify", json={"email": "rlv@x.com", "code": "000000"})
    r = await client.post("/auth/login/code/verify", json={"email": "rlv@x.com", "code": "000000"})
    assert r.status_code == 429
```

（文件头补充 `from conftest import register_user, set_email_verified`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k "code_login"`
Expected: FAIL（404 on 两个新端点）

- [ ] **Step 3: 实现**

`app/schemas/auth.py` 追加：

```python
class LoginCodeRequest(BaseModel):
    email: EmailStr


class CodeLoginVerify(BaseModel):
    email: EmailStr
    code: str = CODE_FIELD
    fingerprint: str | None = None
```

`app/routers/auth.py`：schemas import 行加 `LoginCodeRequest, CodeLoginVerify`；`@router.post("/login", ...)` 之前插入：

```python
@router.post("/login/code/request")
def request_login_code(
    body: LoginCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.services import ratelimit
    ip = _client_ip(request)
    ratelimit.check(f"logcode_ip:{ip}", limit=10, window_seconds=3600)
    email = body.email.lower().strip()
    ratelimit.check(f"logcode:{email}", limit=3, window_seconds=3600)
    ratelimit.check(f"logcode_cd:{email}", limit=1, window_seconds=60)
    # 决策 A：未注册即 404，账号创建只走注册入口（所有账号都有密码）。
    if not db.scalar(select(User).where(User.email == email)):
        raise GapiError(404, "account_not_found", "该邮箱尚未注册")
    code = emailcode.issue_code(db, email=email, purpose="login", ip=ip)
    if not mailer.send_code_email(email, code, purpose="login"):
        emailcode.invalidate_codes(db, email=email, purpose="login")
        raise GapiError(503, "email_send_failed", "验证邮件发送失败，请稍后再试")
    return {"detail": "验证码已发送，请查收", "expires_in": emailcode.CODE_TTL_SECONDS}


@router.post("/login/code/verify", response_model=TokenResponse)
def login_with_code(
    body: CodeLoginVerify,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    from app.services import coin, ratelimit
    ratelimit.check(f"codeverify:{_client_ip(request)}", limit=20, window_seconds=3600)
    email = body.email.lower().strip()
    db.execute(text("BEGIN IMMEDIATE"))
    # 统一 401：不区分无码/错码/过期/锁码，防侧信道枚举。
    emailcode.consume_code(db, email=email, purpose="login", code=body.code, error_status=401)
    user = db.scalar(select(User).where(User.email == email))
    if user is None:  # 发码后账号被删的竞态
        raise GapiError(401, "invalid_code", "验证码错误或已过期")
    if not user.is_active:
        raise GapiError(403, "account_inactive", "Account is deactivated")
    if not user.email_verified:
        user.email_verified = True  # 验证码即邮箱所有权证明（遗留账号补验）
    fingerprint_changed = _register_fingerprint(db, user, body.fingerprint)
    # 单次 commit 覆盖：码消费 + 补验 + 指纹绑定。必须先于每日签到——
    # award_daily_bonus 内部的 BEGIN IMMEDIATE 会隐式回滚未提交写入。
    db.commit()
    coin.award_daily_bonus(db, user)
    result = _issue(user)
    _set_session(response, result.access_token, request)
    return result
```

注意：密码登录只在 `fingerprint_changed` 时 commit，这里**必须无条件 commit**（consume 成功路径只 flush）。`fingerprint_changed` 变量保留赋值以镜像密码登录结构，但不再作为 commit 条件。

- [ ] **Step 4: 跑测试（本文件 + 全套）**

```bash
PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q
PYTHONPATH=$(pwd) .venv/bin/pytest -q
```
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/auth.py app/routers/auth.py tests/test_phase14_email_code.py
git commit -m "feat: passwordless email code login (dual-track)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 补验改造（resend 发码 + verify-email-code）+ 清理 send_verification_email

**Files:**
- Modify: `app/schemas/auth.py`（新增 `VerifyEmailCodeIn`）
- Modify: `app/routers/auth.py`（改造 `/auth/resend-verification`；新增 `/auth/verify-email-code`；删除 `send_verification_email` import）
- Modify: `app/services/email.py`（删除 `send_verification_email`——调用方清零）
- Modify: `tests/test_phase7_features.py`（`test_resend_verification_flow` 终版改写）
- Test: `tests/test_phase14_email_code.py`

**Interfaces:**
- Consumes: 前面全部；`app.deps.get_current_user`
- Produces：
  - `POST /auth/resend-verification`（JWT）：已验证 → `{detail:"邮箱已验证，无需重发"}`；否则发 purpose="verify" 码；限流 `resend:{uid}` 3/h + `resend_cd:{uid}` 1/60s；503 `email_send_failed`。**不再**删 `EmailVerification` 行（在途老链接保持可验证）
  - `POST /auth/verify-email-code`（JWT）：body `{code}`；已验证 → `{detail:"邮箱已验证"}`；否则 consume（purpose="verify"，email=当前用户）→ 置 True；400 `invalid_code`。**不发注册奖励**
  - `GET /auth/verify-email`（不动）：老链接在 24h 窗口内仍可验证 + 发奖（仅限从未验证过的账号）

- [ ] **Step 1: 写失败测试**（追加）

```python
# ── legacy re-verification (code) + link regression ───────────────

async def test_resend_sends_code_and_verify_email_code_works(client, captured_codes):
    await register_user(client, "rv@x.com")
    set_email_verified("rv@x.com", False)
    login = await client.post("/auth/login", json={"email": "rv@x.com", "password": "hunter2hunter"})
    jwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/auth/resend-verification", headers=jwt)
    assert r.status_code == 200
    assert captured_codes[-1][0] == "rv@x.com"
    assert captured_codes[-1][2] == "verify"
    code = captured_codes[-1][1]
    r2 = await client.post("/auth/verify-email-code", json={"code": code}, headers=jwt)
    assert r2.status_code == 200
    db = SessionLocal()
    try:
        assert db.scalar(text("SELECT email_verified FROM users WHERE email='rv@x.com'")) == 1
        # 补验不发注册奖励
        txs = db.scalars(select(CreditTransaction).where(CreditTransaction.tx_type == "signup_bonus")).all()
        assert len(txs) == 1
    finally:
        db.close()
    # 已验证后两个端点都短路
    assert (await client.post("/auth/resend-verification", headers=jwt)).json()["detail"] == "邮箱已验证，无需重发"
    assert (await client.post("/auth/verify-email-code", json={"code": code}, headers=jwt)).json()["detail"] == "邮箱已验证"


async def test_verify_email_code_rejects_wrong_and_foreign(client, captured_codes):
    await register_user(client, "vw@x.com")
    set_email_verified("vw@x.com", False)
    login = await client.post("/auth/login", json={"email": "vw@x.com", "password": "hunter2hunter"})
    jwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/auth/verify-email-code", json={"code": "123456"}, headers=jwt)
    assert r.status_code == 400
    # 别人 purpose=login 的码不能拿来补验
    foreign = issue_code_for("vw@x.com", purpose="login")
    r2 = await client.post("/auth/verify-email-code", json={"code": foreign}, headers=jwt)
    assert r2.status_code == 400


async def test_legacy_link_still_verifies_without_double_bonus(client):
    """在途老链接回归：未验证老账号点链接 → 验证 + 一笔奖励；
    已验证账号持有效老 token → 200 但余额不变（Review Focus #5）。"""
    from app.models import EmailVerification
    # 场景 1：升级前注册的未验证账号（直插库模拟），余额 0
    db = SessionLocal()
    try:
        from app.security import hash_password
        u = User(email="old@x.com", password_hash=hash_password("password123"),
                 role="user", gavincoin_balance=0, email_verified=False)
        db.add(u); db.flush()
        ev = EmailVerification(user_id=u.id)
        db.add(ev); db.commit(); db.refresh(ev)
        token = ev.token
    finally:
        db.close()
    r = await client.get(f"/auth/verify-email?token={token}")
    assert r.status_code == 200
    assert r.json().get("bonus") == "20"
    # 场景 2：已验证用户持有效老 token（边缘）→ 不二次入账
    db = SessionLocal()
    try:
        u2 = db.scalar(select(User).where(User.email == "old@x.com"))
        ev2 = EmailVerification(user_id=u2.id)
        db.add(ev2); db.commit(); db.refresh(ev2)
        token2 = ev2.token
    finally:
        db.close()
    r2 = await client.get(f"/auth/verify-email?token={token2}")
    assert r2.status_code == 200
    db = SessionLocal()
    try:
        u2 = db.scalar(select(User).where(User.email == "old@x.com"))
        assert str(u2.gavincoin_balance) == "20"  # 没再发
    finally:
        db.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest tests/test_phase14_email_code.py -q -k "resend or legacy_link or verify_email_code"`
Expected: FAIL（`/auth/verify-email-code` 404；resend 不发码）

- [ ] **Step 3: 实现**

`app/schemas/auth.py` 追加：

```python
class VerifyEmailCodeIn(BaseModel):
    code: str = CODE_FIELD
```

`app/routers/auth.py`：`resend_verification` 整体替换 + 新端点：

```python
@router.post("/resend-verification")
def resend_verification(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重发验证（6 位码）。在途老链接不受影响（EmailVerification 行保留）。"""
    from app.services import ratelimit
    if user.email_verified:
        return {"detail": "邮箱已验证，无需重发"}
    ratelimit.check(f"resend:{user.id}", limit=3, window_seconds=3600)
    ratelimit.check(f"resend_cd:{user.id}", limit=1, window_seconds=60)
    code = emailcode.issue_code(db, email=user.email, purpose="verify", ip=_client_ip(request))
    if not mailer.send_code_email(user.email, code, purpose="verify"):
        emailcode.invalidate_codes(db, email=user.email, purpose="verify")
        raise GapiError(503, "email_send_failed", "验证邮件发送失败，请稍后再试")
    return {"detail": "验证邮件已发送，请查收", "expires_in": emailcode.CODE_TTL_SECONDS}


@router.post("/verify-email-code")
def verify_email_code(
    body: VerifyEmailCodeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已登录用户输码补验邮箱。不发注册奖励（奖励只在注册建号时发一次）。"""
    if user.email_verified:
        return {"detail": "邮箱已验证"}
    db.execute(text("BEGIN IMMEDIATE"))
    emailcode.consume_code(db, email=user.email, purpose="verify", code=body.code)
    user.email_verified = True
    db.commit()
    return {"detail": "邮箱已验证"}
```

`app/routers/auth.py` 删除 `from app.services.email import send_verification_email`；`app/services/email.py` 删除 `send_verification_email` 函数（调用方已清零；`EmailVerification` 模型与 `GET /auth/verify-email` 端点**保留**——在途链接仍须可验证）。

schemas import 行加 `VerifyEmailCodeIn`。

- [ ] **Step 4: 终版改写 `test_phase7_features.py::test_resend_verification_flow`**

替换整个函数（覆盖 Task 5 的过渡版）：

```python
async def test_resend_verification_flow(client):
    """新语义：注册即已验证；遗留未验证账号走 resend(发码)+verify-email-code 补验。"""
    from conftest import register_user, set_email_verified

    await register_user(client, "vrf@x.com")
    set_email_verified("vrf@x.com", False)
    login = await client.post("/auth/login", json={"email": "vrf@x.com", "password": "hunter2hunter"})
    ujwt = {"Authorization": f"Bearer {login.json()['access_token']}"}

    sent: list[tuple[str, str, str]] = []
    import app.services.email as mailer
    orig = mailer.send_code_email
    mailer.send_code_email = lambda e, c, p: (sent.append((e, c, p)), True)[1]
    try:
        r = await client.post("/auth/resend-verification", headers=ujwt)
        assert r.status_code == 200
        assert sent and sent[-1][2] == "verify"
        code = sent[-1][1]
        r2 = await client.post("/auth/verify-email-code", json={"code": code}, headers=ujwt)
        assert r2.status_code == 200
        me = await client.get("/auth/me", headers=ujwt)
        assert me.json()["email_verified"] is True
    finally:
        mailer.send_code_email = orig
```

（执行者注意：phase7 文件若已有其他 monkeypatch 风格可沿用；要点是断言 200 + purpose="verify" + 补验成功。Task 5 过渡版里若有"不断言余额"的注释一并删除。）

- [ ] **Step 5: 全套测试确认绿色**

Run: `PYTHONPATH=$(pwd) .venv/bin/pytest -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add app/schemas/auth.py app/routers/auth.py app/services/email.py tests/
git commit -m "feat: code-based re-verification; drop link-email sender

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 前端（登录双 tab + 注册码字段）

**Files:**
- Modify: `web/public/index.html`（双 tab + 码行 + 版本串）
- Modify: `web/public/app.js`（整体重写——文件小，给全文）
- Modify: `web/public/style.css`（追加 tab/code-row 样式）

**Interfaces:**
- Consumes: Task 5/6 全部端点；`window.GapiFingerprint.collect()`（fp.js 既有）
- Produces: 三态表单（register / login-password / login-code）；60s 发码倒计时；404 引导注册

- [ ] **Step 1: 改 `web/public/index.html`**

`?v=20260919a` 三处中 **style.css 与 app.js** 改为 `?v=20260925a`（fp.js 内容未变，保留原版本串）。

`#authForm` 整块替换为：

```html
      <div class="tabs hidden" id="loginTabs">
        <button type="button" class="tab active" id="tabPw">密码登录</button>
        <button type="button" class="tab" id="tabCode">验证码登录</button>
      </div>

      <form id="authForm">
        <label for="email">邮箱</label>
        <input id="email" type="email" autocomplete="username" placeholder="you@example.com" required>

        <label for="password" id="passwordLabel">密码</label>
        <input id="password" type="password" autocomplete="current-password" placeholder="至少 8 位" required>

        <div class="code-row hidden" id="codeRow">
          <input id="code" type="text" inputmode="numeric" pattern="[0-9]{6}" maxlength="6"
                 placeholder="6 位验证码" autocomplete="one-time-code" disabled>
          <button type="button" id="sendCodeBtn" disabled>获取验证码</button>
        </div>

        <button class="primary" id="authBtn" type="submit">注册</button>
      </form>
```

- [ ] **Step 2: 重写 `web/public/app.js`（全文替换）**

```javascript
/* gapi login shell.
 * The only script an anonymous visitor can download. Talks to the public
 * auth endpoints only; every panel API lives behind the session in
 * /panel/assets/. Three form states: register / login-password / login-code.
 */

const $ = (id) => document.getElementById(id);
let mode = 'register';        // 'register' | 'login'
let loginVia = 'password';    // 'password' | 'code'（仅 login 态生效）
let countdown = 0;            // 发码倒计时剩余秒数
let countdownTimer = null;

async function fingerprint() {
  try {
    if (window.GapiFingerprint) return await window.GapiFingerprint.collect();
  } catch (e) { /* fingerprinting is best-effort */ }
  return null;
}

async function postJSON(path, body) {
  const fp = await fingerprint();
  if (fp) body.fingerprint = fp;
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error */ }
  if (!res.ok) {
    const err = new Error(data?.error?.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = data?.error?.code || '';
    throw err;
  }
  return data;
}

function msg(text, kind) {
  const el = $('authMsg');
  el.textContent = text;
  el.className = `msg ${kind}`;
}

/* disabled 而非仅 hidden：hidden 的 required 输入仍会阻塞表单提交（浏览器
 * 原生校验对所有非 disabled 控件生效），disabled 控件完全不参与校验与提交。 */
function setFieldState(input, active) {
  input.disabled = !active;
  input.required = active;
}

function render() {
  const isReg = mode === 'register';
  const viaCode = !isReg && loginVia === 'code';

  $('authTitle').textContent = isReg ? '创建账号' : '登录';
  $('authSub').textContent = isReg ? '注册即可获得启动额度' : '登录以管理密钥与账单';
  $('authBtn').textContent = isReg ? '注册' : '登录';
  $('password').autocomplete = isReg ? 'new-password' : 'current-password';
  $('bonusNote').classList.toggle('hidden', !isReg);
  $('loginTabs').classList.toggle('hidden', isReg);
  $('tabPw').classList.toggle('active', loginVia === 'password');
  $('tabCode').classList.toggle('active', viaCode);

  // 密码：注册与密码登录生效；验证码登录隐藏
  const pwActive = !viaCode;
  $('passwordLabel').classList.toggle('hidden', viaCode);
  $('password').classList.toggle('hidden', viaCode);
  setFieldState($('password'), pwActive);

  // 验证码行：注册与验证码登录生效；密码登录隐藏
  const codeActive = isReg || viaCode;
  $('codeRow').classList.toggle('hidden', !codeActive);
  setFieldState($('code'), codeActive);
  $('sendCodeBtn').disabled = !codeActive || countdown > 0;

  $('authSwitch').innerHTML = isReg
    ? '已有账号？<a id="toLogin">登录</a>'
    : '还没有账号？<a id="toLogin">注册</a>';
  $('toLogin').onclick = () => { msg('', ''); mode = isReg ? 'login' : 'register'; render(); };
}

function startCountdown() {
  countdown = 60;
  $('sendCodeBtn').disabled = true;
  $('sendCodeBtn').textContent = `${countdown}s 后重发`;
  countdownTimer = setInterval(() => {
    countdown -= 1;
    if (countdown <= 0) {
      clearInterval(countdownTimer);
      countdownTimer = null;
      $('sendCodeBtn').disabled = false;
      $('sendCodeBtn').textContent = '获取验证码';
    } else {
      $('sendCodeBtn').textContent = `${countdown}s 后重发`;
    }
  }, 1000);
}

$('tabPw').onclick = () => { loginVia = 'password'; msg('', ''); render(); };
$('tabCode').onclick = () => { loginVia = 'code'; msg('', ''); render(); };

$('sendCodeBtn').onclick = async () => {
  const emailInput = $('email');
  if (!emailInput.reportValidity()) return;
  const email = emailInput.value.trim();
  const path = mode === 'register' ? '/auth/register/code/request' : '/auth/login/code/request';
  $('sendCodeBtn').disabled = true;
  msg('发送中…', '');
  try {
    await postJSON(path, { email });
    msg('验证码已发送，请查收邮箱（10 分钟内有效）', 'ok');
    startCountdown();
  } catch (err) {
    $('sendCodeBtn').disabled = false;
    if (err.code === 'account_not_found') {
      msg('该邮箱尚未注册，请点下方链接切换到注册', 'err');
    } else {
      msg(err.message, 'err');
    }
  }
};

$('authForm').onsubmit = async (e) => {
  e.preventDefault();
  const email = $('email').value.trim();
  const password = $('password').value;
  const code = $('code').value.trim();
  const viaCode = mode === 'login' && loginVia === 'code';

  if (mode === 'register' && password.length < 8) return msg('密码至少 8 位', 'err');
  if ((mode === 'register' || viaCode) && !/^\d{6}$/.test(code)) return msg('请输入 6 位数字验证码', 'err');

  $('authBtn').disabled = true;
  msg(mode === 'register' ? '注册中…' : '登录中…', '');
  try {
    if (mode === 'register') {
      await postJSON('/auth/register', { email, password, code });
    } else if (viaCode) {
      await postJSON('/auth/login/code/verify', { email, code });
    } else {
      await postJSON('/auth/login', { email, password });
    }
    // 会话 cookie 为 HttpOnly，浏览器自动携带；跳转受保护面板壳。
    window.location.assign('/panel');
  } catch (err) {
    msg(err.message, 'err');
  } finally {
    $('authBtn').disabled = false;
  }
};

(async function boot() {
  try {
    const cfg = await (await fetch('/config')).json();
    $('bonusAmt').textContent = Number(cfg.bonus).toFixed(0);
  } catch { /* defaults in the markup are fine */ }

  try {
    const res = await fetch('/auth/me');
    if (res.ok) { window.location.replace('/panel'); return; }
  } catch { /* offline / fresh visitor → show shell */ }

  $('auth').classList.remove('hidden');
  render();
})();
```

- [ ] **Step 3: 追加 `web/public/style.css` 样式**

先读 `style.css` 顶部 `:root` 变量块，确认实际变量名（已确认存在 `--accent/--red/--green`；下面用到的 `--muted/--border` 若不存在，改用已有的近似变量）。追加到 `.bonus` 规则附近：

```css
.tabs { display: flex; gap: 8px; margin: 16px 0 4px; }
.tab {
  flex: 1; padding: 8px 0; font-size: 14px; cursor: pointer;
  background: transparent; color: var(--muted, #888);
  border: 1px solid var(--border, #444); border-radius: 8px;
}
.tab.active { color: var(--accent); border-color: var(--accent); }
.code-row { display: flex; gap: 8px; margin-bottom: 4px; }
.code-row input { flex: 1; min-width: 0; }
.code-row button { white-space: nowrap; }
```

（`--muted/--border` 带了兜底色，但**优先改用项目已有变量**——CLAUDE.md 纪律：新增颜色必须走变量。若项目已有 `.hidden` 之外的等效工具类也优先复用。）

- [ ] **Step 4: 冒烟验证（真实进程）**

```bash
.venv/bin/uvicorn app.main:app --port 3099 &
sleep 2
curl -s http://localhost:3099/ | grep -c 'id="loginTabs"'          # → 1
curl -s http://localhost:3099/ | grep -c 'v=20260925a'             # → 2
curl -s -X POST http://localhost:3099/auth/login/code/request \
  -H 'Content-Type: application/json' -d '{"email":"nobody@x.com"}' -i | head -20
# → 404 {"error":{"code":"account_not_found",...}} 且带 X-Gapi-Error: 1
curl -s -X POST http://localhost:3099/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"smoke@x.com","password":"password123","code":"000000"}' -i | head -20
# → 400 invalid_code（无活码；不会触达 SMTP）
kill %1
```

Expected: 页面标记存在、版本串生效、错误契约正确。注意**不要**用真实邮箱触发 register/code/request（会向真实 SMTP 发信，见 `.env`）。

- [ ] **Step 5: Commit**

```bash
git add web/public/index.html web/public/app.js web/public/style.css
git commit -m "feat: dual-track login UI with code tab and countdown

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: 文档同步 + 全量回归

**Files:**
- Modify: `docs/design.md`（认证流程小节）
- Modify: `README.md`（若提及注册/验证流程）
- Modify: `CLAUDE.md`（"两种认证"段 + 限流句）
- Modify: `.env.example`（邮件从"可选"改为"注册/登录必需"的注释）

- [ ] **Step 1: 同步文档**

- `docs/design.md`：`grep -n "verify-email\|邮箱验证\|resend\|注册奖励" docs/design.md` 定位相关小节，改写为：注册=邮箱+密码+6 位码（当场验证当场发奖）；登录双轨（密码 / 验证码）；补验=resend 发码 + verify-email-code；`GET /auth/verify-email` 仅为在途老链接保留；邮件单向纯外发、失败 503 可见。
- `README.md`：若有注册/登录流程描述，同步双轨与收码。
- `CLAUDE.md`："两种认证"段补一句验证码登录；限流句补 code 端点限流键（regcode/logcode/codeverify）。
- `.env.example` 邮件段注释改为：`# 验证邮件（必需：注册与验证码登录依赖外发邮件；GAPI_EMAIL_ENABLED=false 时这些入口返回 503）`。

- [ ] **Step 2: 全量回归 + 覆盖率自检**

```bash
PYTHONPATH=$(pwd) .venv/bin/pytest -q
grep -c "async def test" tests/test_phase14_email_code.py
```
Expected: 全 PASS；phase14 ≥ 20 个用例。

- [ ] **Step 3: Commit**

```bash
git add docs/design.md README.md CLAUDE.md .env.example
git commit -m "docs: sync email code auth flow across design/readme/claude

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 结论（计划落盘前已核对）

- **Spec 覆盖**：spec §3-§11 每条均可指到 Task（§4→T1，§5.1→T2，§5.2→T3/T7，§6.1-6.2→T5，§6.3-6.4→T6，§6.5-6.6→T7，§7→T2/T5/T6 测试，§8→T8（panel 部分见下），§9→T1/T7，§10→各 T 的测试步 + T9 回归，§11→T8 冒烟注意事项/T9 文档）。
- **对 spec 的两处偏离（实现期发现，已向用户因素内化）**：① panel.js 当前**无任何邮箱验证 UI**（grep 证实），且用户确认数据库为空、无遗留未验证账号 → spec §8 的 panel 改动无落点，按 YAGNI 不建；后端补验端点照做。② `send_verification_email` 在 T7 后调用方清零 → 删除而非保留（spec §5.2 原意是兼容，实际无兼容对象）。
- **类型一致**：`issue_code/invalidate_codes/consume_code/generate_code` 签名在 T2 定义、T4/T5/T6/T7 使用，逐字一致；`CODE_TTL_SECONDS/MAX_ATTEMPTS` 常量名一致；`CODE_FIELD` 在 T5 定义、T6/T7 复用。
- **占位符扫描**：无 TBD/TODO/"适当处理"；每个代码步均为完整可落盘内容。
- **Review Focus 5 条**均有归属 Task 的钉住测试（见各条尾注）。
