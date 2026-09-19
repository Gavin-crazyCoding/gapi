"""Shared rich console + theme for the admin CLI.

GavinCoin amounts are rendered with the ◎ symbol, positive green and negative
red, so a ledger reads at a glance.
"""

from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.theme import Theme

COIN = "◎"

GAPI_THEME = Theme(
    {
        "coin": "bold yellow",
        "coin.positive": "bold green",
        "coin.negative": "bold red",
        "user": "cyan",
        "model": "magenta",
        "muted": "dim white",
        "success": "bold green",
        "error": "bold red",
        "warn": "bold yellow",
    }
)

console = Console(theme=GAPI_THEME)


def money(amount: Decimal | float | str, *, signed: bool = False) -> str:
    """Render an amount as markup, coloured by sign when `signed`."""
    value = Decimal(str(amount))
    # Trim the stored six-decimal scale down to something readable, but never
    # round away a non-zero amount into "0".
    text = f"{value.normalize():f}" if value == value.to_integral_value() else f"{value:.6f}".rstrip("0")
    text = text.rstrip(".")
    if not signed:
        return f"[coin]{COIN} {text}[/coin]"
    style = "coin.positive" if value >= 0 else "coin.negative"
    sign = "+" if value > 0 else ""
    return f"[{style}]{sign}{COIN} {text}[/{style}]"


def ok(message: str) -> None:
    console.print(f"[success]✓[/success] {message}")


def fail(message: str) -> None:
    console.print(f"[error]✗ {message}[/error]")


def warn(message: str) -> None:
    console.print(f"[warn]! {message}[/warn]")
