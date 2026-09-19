"""``manage.py user`` — list, activate, deactivate."""

from __future__ import annotations

import typer
from rich.table import Table
from sqlalchemy import select

from app.models import User
from cli.commands._common import find_user, session
from cli.console import console, money, ok

app = typer.Typer(help="User account operations", no_args_is_help=True)


@app.command("list")
def list_users():
    """List every account with its role, status and balance."""
    with session() as db:
        users = db.scalars(select(User).order_by(User.created_at)).all()
        rows = [
            (u.email, u.role, u.is_active, u.gavincoin_balance, u.created_at)
            for u in users
        ]

    if not rows:
        console.print("[muted]No users registered yet[/muted]")
        return

    table = Table(title="Users", show_lines=False, title_style="user")
    table.add_column("Email", style="user")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Balance", justify="right")
    table.add_column("Created", style="muted", no_wrap=True)

    for email, role, active, balance, created in rows:
        table.add_row(
            email,
            "[warn]admin[/warn]" if role == "admin" else role,
            "[success]active[/success]" if active else "[error]disabled[/error]",
            money(balance),
            created.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@app.command()
def deactivate(email: str = typer.Argument(..., help="Account to disable")):
    """Disable an account: login is refused and its keys stop working."""
    with session() as db:
        user = find_user(db, email)
        if not user.is_active:
            console.print(f"[warn]! {email} is already disabled[/warn]")
            raise typer.Exit()
        user.is_active = False
        db.commit()
    ok(f"Deactivated [user]{email}[/user]")


@app.command()
def fingerprints(email: str = typer.Argument(..., help="Account to inspect")):
    """List an account's bound browser fingerprints."""
    from app.models import UserFingerprint

    with session() as db:
        account = find_user(db, email)
        rows = db.scalars(
            select(UserFingerprint)
            .where(UserFingerprint.user_id == account.id)
            .order_by(UserFingerprint.last_seen_at.desc())
        ).all()
        data = [(r.fp_hash, r.created_at, r.last_seen_at) for r in rows]

    if not data:
        console.print(f"[muted]No fingerprints bound to[/muted] [user]{email}[/user]")
        return
    table = Table(title=f"Fingerprints — {email}", title_style="user")
    table.add_column("Hash", style="muted")
    table.add_column("Bound", no_wrap=True)
    table.add_column("Last seen", no_wrap=True)
    for h, created, seen in data:
        table.add_row(h[:16] + "…", created.strftime("%Y-%m-%d %H:%M"), seen.strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@app.command("clear-fingerprint")
def clear_fingerprint(email: str = typer.Argument(..., help="Account to unbind")):
    """Clear all fingerprint bindings (the user re-binds at next login)."""
    from app.services import fingerprint as fp_service

    with session() as db:
        account = find_user(db, email)
        removed = fp_service.clear(db, account.id)
        db.commit()
    ok(f"Cleared {removed} fingerprint binding(s) for [user]{email}[/user]")


@app.command()
def activate(email: str = typer.Argument(..., help="Account to re-enable")):
    """Re-enable a previously disabled account."""
    with session() as db:
        user = find_user(db, email)
        if user.is_active:
            console.print(f"[warn]! {email} is already active[/warn]")
            raise typer.Exit()
        user.is_active = True
        db.commit()
    ok(f"Activated [user]{email}[/user]")
