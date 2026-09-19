"""Simple email sending utilities for gapi."""

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("gapi.email")

# Ports whose handshake is TLS from the first byte (SMTPS), as opposed to
# STARTTLS-upgraded plain SMTP.
_SSL_PORTS = {465}
_CONNECT_TIMEOUT = 10.0


def send_verification_email(email: str, token: str) -> None:
    """Send a verification link to the given e‑mail address.

    In production you would replace this with a third‑party provider (SendGrid,
    Mailgun, etc.). Here we use a plain SMTP server configured via ``settings``.

    If outbound mail is disabled or the SMTP server is unavailable (local dev,
    tests), log a warning and continue — registration must not depend on a
    reachable mail server.
    """
    if not settings.email_enabled:
        log.info("email disabled; verification link for %s not sent", email)
        return

    msg = EmailMessage()
    msg["Subject"] = "请验证您的 gapi 账号"
    msg["From"] = settings.email_sender
    msg["To"] = email
    link = f"{settings.base_url}/auth/verify-email?token={token}"
    msg.set_content(
        f"点击下面的链接完成邮箱验证（链接 24 小时内有效）：\n{link}\n如果不是您本人操作，请忽略此邮件。",
        subtype="plain",
    )
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
    except (OSError, smtplib.SMTPException) as exc:
        # OSError: connection refused/timeout. SMTPException: auth failure,
        # protocol errors. Neither may break registration.
        log.warning("SMTP delivery failed (email not sent): %s", exc)
