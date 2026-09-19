"""/user/keys CRUD (JWT-protected) plus a key-authenticated debug endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import KeyPrincipal, get_current_user, get_key_principal
from app.errors import not_found
from app.models import ApiKey, UsageRecord, User
from app.schemas.key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.security import generate_api_key

router = APIRouter(prefix="/user/keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
def list_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    ).all()
    # One grouped query for every key's rolling 30-day usage, then zip by id.
    since = datetime.now(timezone.utc) - timedelta(days=30)
    usage = {
        key_id: (requests, tokens)
        for key_id, requests, tokens in db.execute(
            select(
                UsageRecord.api_key_id,
                func.count(),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            )
            .where(
                UsageRecord.user_id == user.id,
                UsageRecord.api_key_id.isnot(None),
                UsageRecord.created_at >= since,
            )
            .group_by(UsageRecord.api_key_id)
        ).all()
    }
    out: list[ApiKeyOut] = []
    for row in rows:
        item = ApiKeyOut.model_validate(row)
        item.requests_30d, item.tokens_30d = usage.get(row.id, (0, 0))
        out.append(item)
    return out


@router.post("", response_model=ApiKeyCreated, status_code=201)
def create_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plaintext, key_hash, key_prefix = generate_api_key()
    record = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=body.name,
        quota=body.quota,
        expires_at=body.expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    data = ApiKeyOut.model_validate(record).model_dump()
    return ApiKeyCreated(**data, key=plaintext)


@router.delete("/{key_id}", status_code=204)
def delete_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = db.get(ApiKey, key_id)
    if record is None or record.user_id != user.id:
        raise not_found("api_key_not_found", "API key not found")
    db.delete(record)
    db.commit()


# Key-authenticated (not JWT): proves a gapi key works end-to-end. Phase 1
# acceptance uses this; the /v1 proxy surface in Phase 2 uses the same dep.
debug_router = APIRouter(prefix="/user", tags=["api-keys"])


@debug_router.get("/verify-key")
def verify_key(principal: KeyPrincipal = Depends(get_key_principal)):
    return {
        "key_prefix": principal.api_key.key_prefix,
        "key_name": principal.api_key.name,
        "user": {"id": principal.user.id, "email": principal.user.email, "role": principal.user.role},
    }
