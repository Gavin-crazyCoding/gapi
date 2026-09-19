"""Panel playground: try models in the browser without an API key.

JWT (panel session) authenticated, unlike /v1/* which needs a gapi key.
The same proxy/billing pipeline runs; usage is recorded against the user
with endpoint label "/playground/chat" and a NULL api_key_id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.routers.proxy import run_proxy

router = APIRouter(tags=["playground"])


@router.api_route(
    "/user/playground/chat",
    methods=["POST"],
    include_in_schema=False,
)
async def playground_chat(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await run_proxy(
        "/v1/chat/completions",
        request,
        user,
        None,
        db,
        record_endpoint="/playground/chat",
    )
