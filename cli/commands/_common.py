"""Shared helpers for CLI commands: session handling and user lookup."""

from __future__ import annotations

from contextlib import contextmanager

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User
from cli.console import fail


@contextmanager
def session() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def find_user(db: Session, email: str) -> User:
    """Look a user up by email, exiting with a red message if absent."""
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        fail(f"No user with email {email!r}")
        raise typer.Exit(code=1)
    return user
