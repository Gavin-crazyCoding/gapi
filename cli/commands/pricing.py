"""``manage.py pricing`` — set and list per-model token rates."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import typer
from rich.table import Table
from sqlalchemy import select

from app.models import Pricing
from app.services import billing
from cli.commands._common import session
from cli.console import console, ok

app = typer.Typer(help="Per-model pricing table", no_args_is_help=True)


def _decimal(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        console.print(f"[error]✗ {label} {raw!r} is not a valid rate[/error]")
        raise typer.Exit(code=1)
    if value < 0:
        console.print(f"[error]✗ {label} must not be negative[/error]")
        raise typer.Exit(code=1)
    return value


@app.command("set")
def set_price(
    model: str = typer.Argument(..., help="Model id, e.g. gpt-4o"),
    input_rate: str = typer.Option(..., "--input", help="GavinCoin per 1k input tokens"),
    output_rate: str = typer.Option(..., "--output", help="GavinCoin per 1k output tokens"),
    flat_fee: str = typer.Option(None, "--flat-fee", help="Per-call fee for media endpoints (overrides token billing)"),
):
    """Create or update the rate for a model."""
    inp = _decimal(input_rate, "--input")
    outp = _decimal(output_rate, "--output")
    flat = _decimal(flat_fee, "--flat-fee") if flat_fee is not None else None

    with session() as db:
        row = db.scalar(select(Pricing).where(Pricing.model == model))
        if row is None:
            db.add(Pricing(model=model, input_per_1k=inp, output_per_1k=outp, flat_fee_coin=flat))
            action = "Added"
        else:
            row.input_per_1k, row.output_per_1k = inp, outp
            if flat is not None:
                row.flat_fee_coin = flat
            action = "Updated"
        db.commit()
    suffix = f" flat={flat}/call" if flat is not None else ""
    ok(f"{action} pricing for [model]{model}[/model]: in={inp} out={outp} per 1k tokens{suffix}")


@app.command("list")
def list_prices():
    """Show the full pricing table."""
    with session() as db:
        rows = db.scalars(select(Pricing).order_by(Pricing.model)).all()
        data = [(r.model, r.input_per_1k, r.output_per_1k, r.updated_at) for r in rows]

    if not data:
        console.print(
            "[muted]Pricing table is empty; unpriced models fall back to "
            f"in={billing.FALLBACK_INPUT} out={billing.FALLBACK_OUTPUT}. "
            "Run [/muted][success]manage.py pricing seed[/success][muted] for the defaults.[/muted]"
        )
        return

    table = Table(title="Pricing (GavinCoin per 1k tokens)", title_style="user")
    table.add_column("Model", style="model")
    table.add_column("Input", justify="right", style="coin")
    table.add_column("Output", justify="right", style="coin")
    table.add_column("Updated", style="muted", no_wrap=True)
    for model, inp, outp, updated in data:
        table.add_row(model, f"{inp}", f"{outp}", updated.strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@app.command()
def seed():
    """Insert the documented default rates for models not already priced."""
    with session() as db:
        added = billing.seed_default_pricing(db)
    if added:
        ok(f"Seeded {added} default model rate(s)")
    else:
        console.print("[muted]All default models already priced[/muted]")


@app.command()
def sync(
    input_rate: str = typer.Option("0.005", "--input", help="Rate for newly added models"),
    output_rate: str = typer.Option("0.015", "--output", help="Rate for newly added models"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change, write nothing"),
):
    """Add pricing rows for upstream models that have none.

    Existing rows are never overwritten — re-running this is safe and will not
    silently reset a rate you set by hand.
    """
    import asyncio

    from app.services.catalog import get_catalog
    from app.services.upstream import close_client

    async def fetch():
        try:
            return await get_catalog(force=True)
        finally:
            await close_client()

    try:
        catalog = asyncio.run(fetch())
    except Exception as exc:
        console.print(f"[error]✗ Could not reach the upstream catalog: {exc}[/error]")
        raise typer.Exit(code=1)

    inp = _decimal(input_rate, "--input")
    outp = _decimal(output_rate, "--output")

    with session() as db:
        known = {row.model for row in db.scalars(select(Pricing)).all()}
        # Router entries (auto, fusion) resolve to a real model at request time,
        # and that model's row is what bills — pricing them separately would be
        # a rate that never applies.
        ready = [m["id"] for m in catalog.ready() if m.get("execution_status") == "ready"]
        missing = sorted(set(ready) - known)

        if not missing:
            console.print(
                f"[muted]All {len(ready)} ready models already priced[/muted]"
            )
            return

        if dry_run:
            console.print(f"[warn]! Would add {len(missing)} model(s):[/warn]")
            for m in missing:
                console.print(f"  [model]{m}[/model]")
            return

        for model_id in missing:
            db.add(Pricing(model=model_id, input_per_1k=inp, output_per_1k=outp))
        db.commit()

    ok(f"Added {len(missing)} model(s) at in={inp} out={outp} per 1k tokens")
    console.print(f"[muted]{len(known)} existing rate(s) left untouched[/muted]")
