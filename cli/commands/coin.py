"""``manage.py coin`` — grant, balance, history."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import typer
from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import func, select

from app.models import CreditTransaction, TokenPackage
from app.services import billing
from cli.commands._common import find_user, session
from cli.console import COIN, console, money

app = typer.Typer(help="GavinCoin balance operations", no_args_is_help=True)


@app.command()
def grant(
    email: str = typer.Argument(..., help="Account to credit"),
    amount: str = typer.Argument(..., help="Amount of GavinCoin (may be negative)"),
    note: str = typer.Option(None, "--note", help="Reason, stored on the ledger entry"),
):
    """Credit (or debit) an account's GavinCoin balance."""
    try:
        value = Decimal(amount)
    except InvalidOperation:
        console.print(f"[error]✗ {amount!r} is not a valid amount[/error]")
        raise typer.Exit(code=1)

    with session() as db:
        user = find_user(db, email)
        before = user.gavincoin_balance
        after = billing.grant(db, user, value, note)

    body = Align.center(
        f"[user]{email}[/user]\n\n"
        f"{money(before)}  →  {money(after)}\n"
        f"[muted]change[/muted] {money(value, signed=True)}"
        + (f"\n\n[muted]{note}[/muted]" if note else "")
    )
    console.print(Panel(body, title="[success]✓ Granted[/success]", border_style="green"))


@app.command()
def balance(email: str = typer.Argument(..., help="Account to inspect")):
    """Show an account's balance and remaining prepaid tokens."""
    with session() as db:
        user = find_user(db, email)
        tokens = db.scalar(
            select(func.coalesce(func.sum(TokenPackage.remaining_tokens), 0)).where(
                TokenPackage.user_id == user.id
            )
        )
        role = user.role
        bal = user.gavincoin_balance

    console.print(
        Panel(
            f"[muted]email[/muted]    [user]{email}[/user]\n"
            f"[muted]role[/muted]     {role}\n"
            f"[muted]balance[/muted]  {money(bal)}\n"
            f"[muted]tokens[/muted]   {tokens:,} remaining in packages",
            title=f"[coin]{COIN} Balance[/coin]",
            border_style="yellow",
        )
    )


@app.command()
def history(
    email: str = typer.Argument(..., help="Account to inspect"),
    limit: int = typer.Option(20, "--limit", help="How many entries to show"),
):
    """List the most recent ledger entries for an account."""
    with session() as db:
        user = find_user(db, email)
        rows = db.scalars(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user.id)
            .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
            .limit(limit)
        ).all()

    if not rows:
        console.print(f"[muted]No transactions for[/muted] [user]{email}[/user]")
        return

    table = Table(title=f"Ledger — {email}", show_lines=True, title_style="user")
    table.add_column("When", style="muted", no_wrap=True)
    table.add_column("Type")
    table.add_column("Amount", justify="right")
    table.add_column("Balance", justify="right", style="coin")
    table.add_column("Note", style="muted")

    for r in rows:
        table.add_row(
            r.created_at.strftime("%Y-%m-%d %H:%M"),
            r.tx_type,
            money(r.amount, signed=True),
            f"{COIN} {r.balance_after:.6f}".rstrip("0").rstrip("."),
            r.note or "",
        )
    console.print(table)
