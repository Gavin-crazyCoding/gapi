"""``manage.py usage`` — per-account usage reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import typer
from rich.table import Table
from sqlalchemy import func, select

from app.models import UsageRecord
from cli.commands._common import find_user, session
from cli.console import COIN, console, money

app = typer.Typer(help="Usage reporting", no_args_is_help=True)


def _parse_date(raw: str | None, label: str) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        console.print(f"[error]✗ {label} must be YYYY-MM-DD, got {raw!r}[/error]")
        raise typer.Exit(code=1)


@app.command()
def user(
    email: str = typer.Argument(..., help="Account to report on"),
    date_from: str = typer.Option(None, "--from", help="Start date (YYYY-MM-DD, inclusive)"),
    date_to: str = typer.Option(None, "--to", help="End date (YYYY-MM-DD, inclusive)"),
):
    """Summarise an account's usage per model over a date range."""
    start = _parse_date(date_from, "--from")
    end = _parse_date(date_to, "--to")

    with session() as db:
        account = find_user(db, email)
        query = select(
            UsageRecord.model,
            func.count(),
            func.sum(UsageRecord.prompt_tokens),
            func.sum(UsageRecord.completion_tokens),
            func.sum(UsageRecord.gavincoin_cost),
        ).where(UsageRecord.user_id == account.id)
        if start is not None:
            query = query.where(UsageRecord.created_at >= start)
        if end is not None:
            # --to is inclusive: everything before the start of the next day.
            query = query.where(UsageRecord.created_at < end + timedelta(days=1))
        rows = db.execute(query.group_by(UsageRecord.model).order_by(func.sum(UsageRecord.gavincoin_cost).desc())).all()

    if not rows:
        console.print(f"[muted]No usage recorded for[/muted] [user]{email}[/user]")
        return

    span = f"{date_from or 'start'} → {date_to or 'now'}"
    table = Table(title=f"Usage — {email}  [muted]({span})[/muted]", show_lines=True, title_style="user")
    table.add_column("Model", style="model")
    table.add_column("Requests", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")

    total_cost = 0
    total_requests = 0
    for model, count, prompt, completion, cost in rows:
        total_cost += cost or 0
        total_requests += count
        table.add_row(
            model,
            f"{count:,}",
            f"{int(prompt or 0):,}",
            f"{int(completion or 0):,}",
            money(cost or 0),
        )
    table.add_section()
    table.add_row(
        "[muted]total[/muted]", f"[muted]{total_requests:,}[/muted]", "", "", money(total_cost)
    )
    console.print(table)
