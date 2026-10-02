"""可分享的展开状态令牌（HMAC 签名）与访问权限校验。

分享链接只授权令牌里 vid 指向的单个不可变版本，即使之后发布新版本也不越权。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

DEFAULT_TTL = 604800  # 7 天

_ROLE_RANK = {'reader': 1, 'editor': 2, 'admin': 3}


class ShareError(Exception):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def make_token(secret: str, payload: dict, ttl: int = DEFAULT_TTL) -> str:
    body = dict(payload)
    body['exp'] = int(time.time()) + ttl
    encoded = _b64(json.dumps(body, ensure_ascii=False).encode())
    sig = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f'{encoded}.{sig}'


def verify_token(secret: str, token: str) -> dict:
    try:
        encoded, sig = token.rsplit('.', 1)
    except ValueError as exc:
        raise ShareError('令牌格式非法') from exc
    expect = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise ShareError('令牌签名不匹配')
    payload = json.loads(_unb64(encoded))
    if payload.get('exp', 0) < time.time():
        raise ShareError('令牌已过期')
    return payload


def check_access(conn, user: str, spec_name: str, need: str = 'read') -> bool:
    row = conn.execute(
        'SELECT role FROM permissions WHERE user_id=? AND spec_name=?',
        (user, spec_name),
    ).fetchone()
    if not row:
        return False
    need_role = {'read': 'reader', 'write': 'editor', 'admin': 'admin'}[need]
    return _ROLE_RANK.get(row['role'], -1) >= _ROLE_RANK[need_role]


def grant(conn, user: str, spec_name: str, role: str) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO permissions (user_id, spec_name, role) VALUES (?,?,?)',
        (user, spec_name, role),
    )
    conn.commit()
