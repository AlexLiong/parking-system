"""用户与名单仓储（repository/user_repo.py）。

密码使用 PBKDF2-HMAC-SHA256 加盐哈希（标准库 hashlib，跨平台、无额外依赖），
禁止明文存储（NR-安全性）。
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from ..core.timeutil import iso_local, utcnow
from ..repository.db import Database
from ..repository.models import AdminUser, VehicleList

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
        return dk.hex() == hash_hex
    except (ValueError, AttributeError):
        return False


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def get_by_username(self, username: str) -> Optional[AdminUser]:
        stmt = select(AdminUser).where(AdminUser.username == username).limit(1)
        return self.s.execute(stmt).scalar_one_or_none()

    def create(self, *, username: str, password: str, role: str = "admin") -> AdminUser:
        user = AdminUser(username=username, password_hash=hash_password(password), role=role)
        self.s.add(user)
        self.s.flush()
        return user

    def authenticate(self, username: str, password: str) -> Optional[AdminUser]:
        user = self.get_by_username(username)
        if user and verify_password(password, user.password_hash):
            return user
        return None

    def count(self) -> int:
        return len(list(self.s.execute(select(AdminUser)).scalars().all()))


class VehicleListRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def get(self, plate: str) -> Optional[VehicleList]:
        return self.s.get(VehicleList, plate)

    def upsert(self, *, plate: str, list_type: str,
               valid_until: Optional[datetime] = None, note: Optional[str] = None) -> VehicleList:
        row = self.get(plate)
        if row is None:
            row = VehicleList(plate=plate, list_type=list_type, valid_until=valid_until, note=note)
            self.s.add(row)
        else:
            row.list_type = list_type
            row.valid_until = valid_until
            row.note = note
        self.s.flush()
        return row

    def remove(self, plate: str) -> bool:
        row = self.get(plate)
        if row is None:
            return False
        self.s.delete(row)
        self.s.flush()
        return True

    def list(self, list_type: Optional[str] = None) -> list[dict]:
        stmt = select(VehicleList)
        if list_type:
            stmt = stmt.where(VehicleList.list_type == list_type)
        rows = self.s.execute(stmt.order_by(VehicleList.plate)).scalars().all()
        return [{
            "plate": r.plate, "list_type": r.list_type,
            "valid_until": iso_local(r.valid_until),
            "note": r.note,
        } for r in rows]

    def is_active(self, plate: str, types: tuple[str, ...]) -> bool:
        """名单是否命中且未过期（白/黑/月租）。"""
        return self.active_type(plate) in types

    def active_type(self, plate: str, at: Optional[datetime] = None) -> Optional[str]:
        row = self.get(plate)
        if row is None:
            return None
        if row.valid_until is not None and row.valid_until <= (at or utcnow()):
            return None
        return row.list_type
