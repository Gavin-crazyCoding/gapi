"""``manage.py code`` — mint and manage redemption codes."""

from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation

import typer
from rich.table import Table
from sqlalchemy import select

from app.models import RedemptionCode
from cli.commands._common import session
from cli.console import COIN, console, ok

app = typer.Typer(help="Redemption code operations", no_args_is_help=True)

# No 0/O/1/I/L: codes get read out loud and typed by hand.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _mint_code() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "GAVI-" + "-".join(groups)


@app.command()
def create(
    amount: str = typer.Argument(..., help="GavinCoin each code redeems"),
    count: int = typer.Option(1, "--count", "-n", help="How many codes to mint"),
    uses: int = typer.Option(1, "--uses", help="Max redeems per code (>1 = campaign code)"),
    note: str = typer.Option(None, "--note", help="Memo stored on each code"),
):
    """Mint one or more redemption codes."""
    try:
        value = Decimal(amount)
    except InvalidOperation:
        console.print(f"[error]✗ {amount!r} is not a valid amount[/error]")
        raise typer.Exit(code=1)
    if value <= 0:
        console.print("[error]✗ amount must be positive[/error]")
        raise typer.Exit(code=1)
    if count < 1 or uses < 1:
        console.print("[error]✗ --count and --uses must be >= 1[/error]")
        raise typer.Exit(code=1)

    codes: list[str] = []
    with session() as db:
        for _ in range(count):
            while True:
                candidate = _mint_code()
                if db.scalar(
                    select(RedemptionCode).where(RedemptionCode.code == candidate)
                ) is None:
                    break
            db.add(
                RedemptionCode(
                    code=candidate,
                    amount=value,
                    max_uses=uses,
                    note=note,
                    created_by="cli",
                )
            )
            codes.append(candidate)
        db.commit()

    for c in codes:
        console.print(f"  [coin]{c}[/coin]")
    ok(f"Minted {len(codes)} code(s) worth [coin]{COIN} {value}[/coin] each, {uses} use(s) each")


@app.command("list")
def list_codes(
    active_only: bool = typer.Option(False, "--active", help="Hide exhausted/disabled codes"),
):
    """List redemption codes."""
    with session() as db:
        q = select(RedemptionCode).order_by(RedemptionCode.created_at.desc())
        rows = db.scalars(q).all()
        data = [
            (r.code, r.amount, r.used_count, r.max_uses, r.is_active, r.note, r.created_at)
            for r in rows
            if not active_only or (r.is_active and not r.exhausted)
        ]

    if not data:
        console.print("[muted]No redemption codes[/muted]")
        return

    table = Table(title="Redemption codes", show_lines=False, title_style="user")
    table.add_column("Code", style="coin")
    table.add_column("Amount", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Status")
    table.add_column("Note", style="muted")
    table.add_column("Created", style="muted", no_wrap=True)
    for code, amount, used, max_uses, active, note, created in data:
        if not active:
            status = "[error]disabled[/error]"
        elif used >= max_uses:
            status = "[muted]exhausted[/muted]"
        else:
            status = "[success]active[/success]"
        table.add_row(
            code,
            f"{COIN} {amount}",
            f"{used}/{max_uses}",
            status,
            note or "",
            created.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@app.command()
def deactivate(code: str = typer.Argument(..., help="Code to disable")):
    """Disable a code (already-redeemed credit is unaffected)."""
    with session() as db:
        row = db.scalar(
            select(RedemptionCode).where(RedemptionCode.code == code.strip().upper())
        )
        if row is None:
            console.print(f"[error]✗ No code {code!r}[/error]")
            raise typer.Exit(code=1)
        row.is_active = False
        db.commit()
    ok(f"Deactivated [coin]{row.code}[/coin]")
