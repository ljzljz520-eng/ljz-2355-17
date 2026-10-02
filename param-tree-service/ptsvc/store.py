"""SQLite 存储：规范版本、快照、参数目录、示例与反向索引、复验队列、分享、权限。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Any

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  created_at  REAL NOT NULL,
  canonical_json TEXT NOT NULL,
  root_pointer TEXT NOT NULL DEFAULT '#'
);
CREATE TABLE IF NOT EXISTS spec_versions (
  api_name TEXT NOT NULL,
  version  TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  published_at REAL NOT NULL,
  published_by TEXT,
  is_latest INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (api_name, version),
  FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS params (
  snapshot_id TEXT NOT NULL,
  param_path  TEXT NOT NULL,
  name TEXT NOT NULL,
  depth INTEGER NOT NULL,
  kind TEXT NOT NULL,
  requiredness_json TEXT NOT NULL,
  has_default INTEGER NOT NULL DEFAULT 0,
  default_json TEXT,
  nullable INTEGER NOT NULL DEFAULT 0,
  deprecated INTEGER NOT NULL DEFAULT 0,
  value_type TEXT,
  ref_target TEXT,
  diagnostics_json TEXT NOT NULL DEFAULT '[]',
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, param_path)
);
CREATE TABLE IF NOT EXISTS examples (
  example_id TEXT PRIMARY KEY,
  api_name TEXT NOT NULL,
  name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS example_versions (
  example_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  state TEXT NOT NULL,
  checked_at REAL,
  issues_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (example_id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS example_paths (
  example_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  param_path TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (example_id, snapshot_id, param_path)
);
CREATE INDEX IF NOT EXISTS idx_ep_path ON example_paths(snapshot_id, param_path);
CREATE TABLE IF NOT EXISTS revalidation_queue (
  snapshot_id TEXT NOT NULL,
  example_id TEXT NOT NULL,
  reason_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (snapshot_id, example_id)
);
CREATE TABLE IF NOT EXISTS shares (
  share_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  api_name TEXT NOT NULL,
  state_json TEXT NOT NULL,
  scope_hint TEXT,
  created_by TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  api_name TEXT NOT NULL,
  path_pattern TEXT NOT NULL,
  principal TEXT NOT NULL,
  effect TEXT NOT NULL CHECK (effect IN ('allow','deny'))
);
"""


def canonical_hash(doc: Any) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _canonical(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, path: str = ":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 快照 / 版本 ----------
    def save_snapshot(self, doc: Any, root_pointer: str = "#") -> str:
        sid = canonical_hash(doc)
        with self.lock:
            exists = self.conn.execute(
                "SELECT 1 FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
            if not exists:
                self.conn.execute(
                    "INSERT INTO snapshots(snapshot_id,created_at,canonical_json,root_pointer)"
                    " VALUES(?,?,?,?)", (sid, time.time(), _canonical(doc), root_pointer))
                self.conn.commit()
        return sid

    def load_snapshot_doc(self, sid: str) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT canonical_json FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        return json.loads(row["canonical_json"])

    def publish_version(self, api: str, version: str, sid: str,
                        by: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE spec_versions SET is_latest=0 WHERE api_name=?", (api,))
            self.conn.execute(
                "INSERT INTO spec_versions(api_name,version,snapshot_id,published_at,"
                "published_by,is_latest) VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(api_name,version) DO UPDATE SET "
                "snapshot_id=excluded.snapshot_id,published_at=excluded.published_at,"
                "published_by=excluded.published_by,is_latest=1",
                (api, version, sid, time.time(), by))
            self.conn.commit()

    def latest_snapshot(self, api: str) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT snapshot_id FROM spec_versions WHERE api_name=? AND is_latest=1",
                (api,)).fetchone()
        return row["snapshot_id"] if row else None

    def snapshot_for_version(self, api: str, version: str) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT snapshot_id FROM spec_versions WHERE api_name=? AND version=?",
                (api, version)).fetchone()
        return row["snapshot_id"] if row else None

    def versions(self, api: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT version,snapshot_id,published_at,is_latest FROM spec_versions "
                "WHERE api_name=? ORDER BY published_at", (api,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 参数目录 ----------
    def upsert_params(self, sid: str, records: list[dict[str, Any]]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM params WHERE snapshot_id=?", (sid,))
            self.conn.executemany(
                "INSERT INTO params(snapshot_id,param_path,name,depth,kind,"
                "requiredness_json,has_default,default_json,nullable,deprecated,"
                "value_type,ref_target,diagnostics_json,fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(sid, r["param_path"], r["name"], r["depth"], r["kind"],
                  json.dumps(r.get("requiredness", []), ensure_ascii=False),
                  1 if r.get("has_default") else 0,
                  json.dumps(r.get("default"), ensure_ascii=False)
                  if r.get("has_default") else None,
                  1 if r.get("nullable") else 0, 1 if r.get("deprecated") else 0,
                  r.get("value_type"), r.get("ref_target"),
                  json.dumps(r.get("diagnostics", []), ensure_ascii=False),
                  r["fingerprint"]) for r in records])
            self.conn.commit()

    def param_fingerprints(self, sid: str) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT param_path,fingerprint FROM params WHERE snapshot_id=?",
                (sid,)).fetchall()
        return {r["param_path"]: r["fingerprint"] for r in rows}

    def param(self, sid: str, path: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM params WHERE snapshot_id=? AND param_path=?",
                (sid, path)).fetchone()
        return dict(row) if row else None

    def all_params(self, sid: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM params WHERE snapshot_id=? ORDER BY depth,param_path",
                (sid,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 示例 ----------
    def add_example(self, api: str, name: str, payload: Any) -> str:
        eid = "ex_" + uuid.uuid4().hex[:12]
        with self.lock:
            self.conn.execute(
                "INSERT INTO examples(example_id,api_name,name,payload_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (eid, api, name, json.dumps(payload, ensure_ascii=False), time.time()))
            self.conn.commit()
        return eid

    def get_example(self, eid: str) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM examples WHERE example_id=?",
                                    (eid,)).fetchone()
        if not row:
            raise KeyError(eid)
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def examples_for_api(self, api: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT example_id,name FROM examples WHERE api_name=?", (api,)).fetchall()
        return [dict(r) for r in rows]

    def save_example_result(self, eid: str, sid: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO example_versions(example_id,snapshot_id,state,checked_at,"
                "issues_json) VALUES(?,?,?,?,?) "
                "ON CONFLICT(example_id,snapshot_id) DO UPDATE SET state=excluded.state,"
                "checked_at=excluded.checked_at,issues_json=excluded.issues_json",
                (eid, sid, result["state"], time.time(),
                 json.dumps(result.get("issues", []), ensure_ascii=False)))
            self.conn.execute(
                "DELETE FROM example_paths WHERE example_id=? AND snapshot_id=?",
                (eid, sid))
            self.conn.executemany(
                "INSERT INTO example_paths(example_id,snapshot_id,param_path,state)"
                " VALUES(?,?,?,?)",
                [(eid, sid, p, s) for p, s in result.get("paths", {}).items()])
            self.conn.execute(
                "DELETE FROM revalidation_queue WHERE snapshot_id=? AND example_id=?",
                (sid, eid))
            self.conn.commit()

    def example_state(self, eid: str, sid: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT state,checked_at,issues_json FROM example_versions "
                "WHERE example_id=? AND snapshot_id=?", (eid, sid)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["issues"] = json.loads(d.pop("issues_json"))
        return d

    def examples_touching(self, sid: str, paths: set[str]) -> list[str]:
        if not paths:
            return []
        q = ("SELECT DISTINCT example_id FROM example_paths WHERE snapshot_id=? "
             "AND param_path IN (%s)" % ",".join("?" * len(paths)))
        with self.lock:
            rows = self.conn.execute(q, [sid] + list(paths)).fetchall()
        return [r["example_id"] for r in rows]

    def enqueue_revalidation(self, sid: str, eid: str, reason: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO revalidation_queue(snapshot_id,example_id,reason_json,"
                "created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(snapshot_id,example_id) DO UPDATE SET "
                "reason_json=excluded.reason_json,created_at=excluded.created_at",
                (sid, eid, json.dumps(reason, ensure_ascii=False), time.time()))
            self.conn.execute(
                "INSERT INTO example_versions(example_id,snapshot_id,state) "
                "VALUES(?,?,?) ON CONFLICT(example_id,snapshot_id) DO UPDATE SET "
                "state='pending', checked_at=NULL, issues_json='[]'",
                (eid, sid, "pending"))
            self.conn.commit()

    def queue(self, sid: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT example_id,reason_json,created_at FROM revalidation_queue "
                "WHERE snapshot_id=?", (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reason"] = json.loads(d.pop("reason_json"))
            out.append(d)
        return out

    def set_pending_for_new_snapshot(self, eid: str, sid: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO example_versions(example_id,snapshot_id,state)"
                " VALUES(?,?,?)", (eid, sid, "pending"))
            self.conn.commit()

    # ---------- 分享 ----------
    def save_share(self, sid: str, api: str, state: dict[str, Any],
                   scope_hint: str, by: str) -> str:
        share_id = "sh_" + uuid.uuid4().hex[:12]
        with self.lock:
            self.conn.execute(
                "INSERT INTO shares(share_id,snapshot_id,api_name,state_json,"
                "scope_hint,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (share_id, sid, api, json.dumps(state, ensure_ascii=False),
                 scope_hint, by, time.time()))
            self.conn.commit()
        return share_id

    def get_share(self, share_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM shares WHERE share_id=?",
                                    (share_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["state"] = json.loads(d.pop("state_json"))
        return d

    # ---------- 权限 ----------
    def set_policy(self, api: str, pattern: str, principal: str, effect: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO access_policies(api_name,path_pattern,principal,effect)"
                " VALUES(?,?,?,?)", (api, pattern, principal, effect))
            self.conn.commit()

    def policies(self, api: str, principal: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT path_pattern,effect FROM access_policies "
                "WHERE api_name=? AND principal=? ORDER BY id", (api, principal)).fetchall()
        return [dict(r) for r in rows]
