"""gapi administration CLI (console entry point for `gapi-manage`).

Coin grants, pricing, account activation, redemption codes and usage reports
happen here; economy settings, user and announcement management live in the
panel's admin tabs (/admin/*, /users/*). Upstream provider management stays
in the FreeLLM API dashboard.
"""

from __future__ import annotations

import typer

from cli.commands import code as code_cmd, coin, pricing, usage, user

app = typer.Typer(help="gapi administration CLI", no_args_is_help=True)
app.add_typer(coin.app, name="coin")
app.add_typer(user.app, name="user")
app.add_typer(pricing.app, name="pricing")
app.add_typer(usage.app, name="usage")
app.add_typer(code_cmd.app, name="code")


if __name__ == "__main__":
    app()
