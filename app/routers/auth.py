"""/auth/register, /auth/login, /auth/me, /auth/logout."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import SESSION_COOKIE, get_current_user
from app.errors import GapiError
from app.models import CreditTransaction, EmailVerification, User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.security import create_access_token, hash_password, jwt_expire_days, verify_password
from app.services.email import send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])


def _register_fingerprint(db: Session, user: User, fingerprint: str | None) -> bool:
    """Register the login environment as trusted (password already verified).

    A correct password is the stronger proof of identity: an unknown
    fingerprint at login time means the user's browser drifted (zoom, window
    size, UA upgrade) or they are on a new device — bind it, never lock them
    out. The strict check lives in deps.get_current_user, where requests
    arrive *without* a password.
    """
    from app.services import fingerprint as fp_service
    return fp_service.bind(db, user.id, fingerprint)


def _user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


def _issue(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=jwt_expire_days() * 86400,
        user=_user_out(user),
    )


def _set_session(response: Response, token: str, request: Request) -> None:
    """Store the JWT in an HttpOnly cookie.

    HttpOnly keeps it out of JavaScript reach (XSS cannot exfiltrate the
    session); SameSite=Strict neutralises cross-site request forgery.
    Secure is only set when the request itself arrived over HTTPS — local dev
    runs plain HTTP on localhost.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=jwt_expire_days() * 86400,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    """Peer IP for rate-limit keys.

    X-Forwarded-For is client-controlled: honouring it unconditionally lets a
    caller mint a fresh limiter bucket per request and defeat the
    register/login limits entirely. Only trust it when the direct peer is a
    configured trusted proxy (GAPI_TRUSTED_PROXY_IPS); otherwise key on the
    actual peer.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in settings.trusted_proxy_ip_set:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return peer


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    from app.services import coin, ratelimit
    ratelimit.check(f"register:{_client_ip(request)}", limit=20, window_seconds=3600)
    # BEGIN IMMEDIATE takes SQLite's write lock up front (same discipline as
    # billing), so the first-user-is-admin check below and the insert are
    # serialized against a concurrent registration: two racing signups can
    # never both observe an empty users table and both crown themselves admin.
    db.execute(text("BEGIN IMMEDIATE"))
    if not coin.registration_open(db):
        raise GapiError(403, "registration_closed", "当前未开放注册")

    email = body.email.lower().strip()

    if db.scalar(select(User).where(User.email == email)):
        raise GapiError(409, "email_taken", "Email is already registered")

    # First account ever registered becomes the admin (decision D4).
    is_first = db.scalar(select(func.count()).select_from(User)) == 0
    # The admin panel's registration_bonus setting is the live value; the env
    # var is only the fallback baked into coin.DEFAULTS.
    bonus = coin.get_registration_bonus(db)

    user = User(
        email=email,
        password_hash=hash_password(body.password),
        role="admin" if is_first else "user",
        gavincoin_balance=bonus,
    )
    db.add(user)
    try:
        db.flush()  # assigns user.id, still inside the transaction
        if bonus > 0:
            # The signup grant is a ledger event like any other credit, so the
            # opening balance is explainable from credit_transactions alone.
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=bonus,
                    balance_after=bonus,
                    tx_type="signup_bonus",
                    note="registration bonus",
                )
            )
        # 发送验证邮件
        ev = EmailVerification(user_id=user.id)
        db.add(ev)
        if body.fingerprint:
            _register_fingerprint(db, user, body.fingerprint)
        db.commit()
        send_verification_email(email=user.email, token=ev.token)
    except IntegrityError:
        # Two concurrent signups for the same email: the unique index decides.
        db.rollback()
        raise GapiError(409, "email_taken", "Email is already registered")
    db.refresh(user)
    result = _issue(user)
    _set_session(response, result.access_token, request)
    return result


@router.get("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    ev = db.scalar(select(EmailVerification).where(
        EmailVerification.token == token,
        EmailVerification.expires_at > _utcnow(),
    ))
    if not ev:
        raise GapiError(400, "invalid_token", "Verification token is invalid or expired")
    user = db.get(User, ev.user_id)
    if user is None:
        db.delete(ev)
        db.commit()
        raise GapiError(400, "invalid_token", "Account no longer exists")
    user.email_verified = True
    db.delete(ev)
    db.commit()
    return {"detail": "邮箱已验证"}


@router.post("/resend-verification")
def resend_verification(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Issue a fresh verification link (old tokens are revoked)."""
    from app.services import ratelimit
    if user.email_verified:
        return {"detail": "邮箱已验证，无需重发"}
    ratelimit.check(f"resend:{user.id}", limit=3, window_seconds=3600)
    for old_ev in db.scalars(
        select(EmailVerification).where(EmailVerification.user_id == user.id)
    ).all():
        db.delete(old_ev)
    ev = EmailVerification(user_id=user.id)
    db.add(ev)
    db.commit()
    send_verification_email(email=user.email, token=ev.token)
    return {"detail": "验证邮件已发送，请查收"}


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    from app.services import ratelimit
    ratelimit.check(f"login:{_client_ip(request)}", limit=30, window_seconds=300)

    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        # Same message whether the email or the password is wrong.
        raise GapiError(401, "invalid_credentials", "Invalid email or password")
    if not user.is_active:
        raise GapiError(403, "account_inactive", "Account is deactivated")

    fingerprint_changed = _register_fingerprint(db, user, body.fingerprint)

    # Commit fingerprint binding BEFORE check-in, because award_daily_bonus
    # starts its own BEGIN IMMEDIATE transaction (which implicitly rolls back
    # any uncommitted changes). We want the bind to survive.
    if fingerprint_changed:
        db.commit()

    # Daily check-in: shared with POST /user/checkin (rolling 24h window, one
    # guarded UPDATE — a password login and a panel visit can never both win).
    from app.services import coin
    awarded = coin.award_daily_bonus(db, user)

    result = _issue(user)
    _set_session(response, result.access_token, request)
    return result


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return None


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)
