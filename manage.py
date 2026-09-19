"""gapi admin CLI.

Usage: python manage.py --help

Thin wrapper around cli.manage (the real app, also exposed as the
`gapi-manage` console script). Upstream provider management stays in the
FreeLLM API dashboard.
"""

from __future__ import annotations

from cli.manage import app

if __name__ == "__main__":
    app()
