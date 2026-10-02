"""SQLite 连接与初始化。"""
from __future__ import annotations

import os
import secrets
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(os.path.join(_HERE, 'sql', 'schema.sql'), encoding='utf-8') as f:
        conn.executescript(f.read())
    row = conn.execute("SELECT value FROM meta WHERE key='share_secret'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('share_secret', ?)",
            (secrets.token_hex(32),),
        )
        conn.commit()


def get_secret(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM meta WHERE key='share_secret'").fetchone()['value']


def new_memory_db() -> sqlite3.Connection:
    """测试用：内存库。"""
    conn = connect(':memory:')
    init_db(conn)
    return conn
