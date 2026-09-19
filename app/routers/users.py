"""User CRUD under admin control."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import User, get_admin_user
from app.errors import GapiError
from app.models import User as UserModel
from app.security import hash_password
from app.schemas.auth import UserOut

router = APIRouter(prefix="/users", tags=["users"])

# NOTE: these must stay "" (not "/") — the StaticFiles mount at "/" would
# otherwise swallow the slash-redirect and answer 404 from the disk.


class UserCreate(BaseModel):
    email: str = Field(..., pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    password: str = Field(..., min_length=8, max_length=72)
    role: str = Field(default="user", pattern="^(user|admin)$")
    concurrent_limit: int = Field(default=5, ge=1, le=100)


@router.get("", response_model=list[UserOut])
def list_users(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(UserModel).order_by(UserModel.created_at)).all()
    return [UserOut.model_validate(u) for u in rows]


@router.get("/{uid}", response_model=UserOut)
def get_user(
    uid: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(UserModel, uid)
    if user is None:
        raise GapiError(404, "user_not_found", "User not found")
    return UserOut.model_validate(user)


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    email = body.email.lower().strip()
    if db.scalar(select(UserModel).where(UserModel.email == email)):
        raise GapiError(409, "email_taken", "Email is already registered")
    user = UserModel(
        email=email,
        password_hash=hash_password(body.password),
        role=body.role,
        concurrent_limit=body.concurrent_limit,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


class UserPatch(BaseModel):
    email: str | None = None
    role: str | None = Field(None, pattern="^(user|admin)$")
    is_active: bool | None = None
    concurrent_limit: int | None = Field(None, ge=1, le=100)
    grant_balance: Decimal | None = None


@router.put("/{uid}", response_model=UserOut)
def update_user(
    uid: int,
    body: UserPatch,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(UserModel, uid)
    if user is None:
        raise GapiError(404, "user_not_found", "User not found")
    if body.email is not None:
        new_email = body.email.lower().strip()
        if new_email != user.email:
            if db.scalar(select(UserModel).where(UserModel.email == new_email)):
                raise GapiError(409, "email_taken", "Email is already registered")
            user.email = new_email
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.concurrent_limit is not None:
        user.concurrent_limit = body.concurrent_limit
    if body.grant_balance is not None:
        from app.services import billing
        billing.grant(db, user, body.grant_balance, "admin adjustment")
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.delete("/{uid}/fingerprints", status_code=200)
def clear_fingerprints(
    uid: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Unbind all of a user's browser fingerprints (locked-out escape hatch)."""
    from app.services import fingerprint as fp_service
    user = db.get(UserModel, uid)
    if user is None:
        raise GapiError(404, "user_not_found", "User not found")
    removed = fp_service.clear(db, user.id)
    db.commit()
    return {"cleared": removed}


@router.delete("/{uid}", status_code=204)
def delete_user(
    uid: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(UserModel, uid)
    if user is None:
        raise GapiError(404, "user_not_found", "User not found")
    if user.is_admin and db.scalar(
        select(UserModel).where(UserModel.id != uid, UserModel.role == "admin")
    ) is None:
        raise GapiError(400, "cannot_delete_only_admin", "Cannot delete the only admin")
    db.delete(user)
    db.commit()