"""应用服务层：发布、索引、复验编排、会话、权限、分享、版本切换、导出。"""
from __future__ import annotations

import fnmatch
import json
import threading
import uuid
from typing import Any

from . import model as M
from .resolver import Resolver, decode_handle, encode_handle, SnapshotMismatch
from .store import Store
from .validator import ExampleValidator


def node_fingerprint(node: M.Node) -> str:
    payload = {
        "k": node.kind, "t": node.value_type,
        "rq": [r.to_dict() for r in node.requiredness],
        "hd": node.has_default, "df": node.default if node.has_default else None,
        "nu": node.nullable, "dp": node.deprecated, "en": node.enum,
        "ref": node.ref_target,
        "diag": [(d.code, d.message) for d in node.diagnostics],
    }
    import hashlib
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._resolvers: dict[str, Resolver] = {}
        self._sessions: dict[str, "Session"] = {}
        self._lock = threading.RLock()

    # ---------- 权限 ----------
    def make_can_read(self, api: str, principal: str):
        policies = self.store.policies(api, principal)

        def can_read(path: str) -> bool:
            # 默认允许；出现显式规则后按最长匹配/deny 优先
            matched = [p for p in policies
                       if fnmatch.fnmatchcase(path, p["path_pattern"])]
            if not matched:
                return True
            # 最后命中的具体规则生效；同长度 deny 优先
            best = max(len(p["path_pattern"]) for p in matched)
            tied = [p for p in matched if len(p["path_pattern"]) == best]
            return not any(p["effect"] == "deny" for p in tied)
        return can_read

    # ---------- 解析器缓存 ----------
    def resolver(self, sid: str, api: str | None = None,
                 principal: str | None = None) -> Resolver:
        if principal is not None and api is not None:
            doc = self.store.load_snapshot_doc(sid)
            return Resolver(sid, doc, self.make_can_read(api, principal))
        with self._lock:
            if sid not in self._resolvers:
                doc = self.store.load_snapshot_doc(sid)
                self._resolvers[sid] = Resolver(sid, doc)
            return self._resolvers[sid]

    # ---------- 发布：快照 + 索引 + 影响分析 + 复验 ----------
    def publish(self, api: str, version: str, doc: dict[str, Any],
                by: str | None = None) -> dict[str, Any]:
        prev_sid = self.store.latest_snapshot(api)
        sid = self.store.save_snapshot(doc)
        self.store.publish_version(api, version, sid, by)

        # 索引走查（无权限过滤的全量目录）
        res = self.resolver(sid)
        records: list[dict[str, Any]] = []
        index_paths: set[str] = set()
        for path, node in res.iter_index():
            if path == "$":
                continue
            index_paths.add(path)
            records.append({
                "param_path": path, "name": node.name, "depth": node.depth,
                "kind": node.kind, "value_type": node.value_type,
                "requiredness": [r.to_dict() for r in node.requiredness],
                "has_default": node.has_default,
                "default": node.default if node.has_default else None,
                "nullable": node.nullable, "deprecated": node.deprecated,
                "ref_target": node.ref_target,
                "diagnostics": [d.to_dict() for d in node.diagnostics],
                "fingerprint": node_fingerprint(node),
            })
        self.store.upsert_params(sid, records)

        affected: list[str] = []
        untouched: list[str] = []
        enqueued: list[str] = []
        if prev_sid and prev_sid != sid:
            old_fp = self.store.param_fingerprints(prev_sid)
            new_fp = self.store.param_fingerprints(sid)
            changed = {p for p in new_fp.keys() | old_fp.keys()
                       if old_fp.get(p) != new_fp.get(p)}
            # 新增的（无条件或条件）必填路径：旧示例不可能包含该路径，
            # 旧反向索引查不到，必须显式视为“影响所有示例”。
            new_required: set[str] = set()
            for p in changed - set(old_fp):
                rec = self.store.param(sid, p)
                if not rec:
                    continue
                rqs = json.loads(rec["requiredness_json"] or "[]")
                if any(r.get("require") == M.RQ_REQUIRED for r in rqs):
                    new_required.add(p)
            examples = [e["example_id"] for e in self.store.examples_for_api(api)]
            touched = set(self.store.examples_touching(prev_sid, changed))
            for eid in examples:
                old_paths = set(self._example_paths(prev_sid, eid))
                hit = changed & old_paths
                if eid in touched or (new_required):
                    reasons = sorted(hit | new_required)
                    self.store.set_pending_for_new_snapshot(eid, sid)
                    self.store.enqueue_revalidation(
                        sid, eid,
                        {"changed_paths": sorted(hit),
                         "new_required_paths": sorted(new_required)})
                    enqueued.append(eid)
                    affected.extend(reasons)
                else:
                    # 未触达变更：立即重跑，结果明确
                    result = self._validate_example(eid, sid)
                    untouched.append({"example_id": eid, "state": result["state"]})
        return {"snapshot_id": sid, "previous_snapshot_id": prev_sid,
                "param_count": len(records),
                "changed_paths": sorted(set(affected)),
                "revalidation_enqueued": enqueued,
                "untouched_rechecked": untouched}

    def _example_paths(self, sid: str, eid: str) -> list[str]:
        # 经由示例结果反查
        ex = self.store.get_example(eid)
        return list(ExampleValidator(self.resolver(sid)).validate(ex["payload"])["paths"])

    def _validate_example(self, eid: str, sid: str) -> dict[str, Any]:
        ex = self.store.get_example(eid)
        res = self.resolver(sid)
        result = ExampleValidator(res).validate(ex["payload"])
        self.store.save_example_result(eid, sid, result)
        return result

    def add_example(self, api: str, name: str, payload: Any,
                    validate_sid: str | None = None) -> dict[str, Any]:
        eid = self.store.add_example(api, name, payload)
        results = {}
        sid = validate_sid or self.store.latest_snapshot(api)
        if sid:
            results[sid] = self._validate_example(eid, sid)
        return {"example_id": eid, "results": {k: v["state"] for k, v in results.items()}}

    def revalidate(self, eid: str, sid: str) -> dict[str, Any]:
        return self._validate_example(eid, sid)

    # ---------- 会话 ----------
    def create_session(self, api: str, principal: str,
                       version: str | None = None) -> "Session":
        sid = (self.store.snapshot_for_version(api, version) if version
               else self.store.latest_snapshot(api))
        if not sid:
            raise LookupError(f"api/version 未发布: {api}@{version}")
        sess = Session(sid=sid, api=api, principal=principal, service=self)
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def get_session(self, session_id: str) -> "Session":
        with self._lock:
            sess = self._sessions.get(session_id)
        if not sess:
            raise KeyError(session_id)
        return sess

    def share(self, sess: "Session", scope_hint: str = "") -> str:
        return self.store.save_share(
            sess.snapshot_id, sess.api,
            {"expanded": list(sess.expanded), "selection": sess.selection},
            scope_hint, sess.principal)

    def from_share(self, share_id: str, principal: str) -> dict[str, Any]:
        rec = self.store.get_share(share_id)
        if not rec:
            raise KeyError(share_id)
        sess = self.create_session_with_sid(rec["api_name"], rec["snapshot_id"], principal)
        dropped = sess.restore(rec["state"]["expanded"], rec["state"].get("selection"))
        return {"session_id": sess.id, "dropped": dropped,
                "snapshot_id": sess.snapshot_id}

    def create_session_with_sid(self, api: str, sid: str, principal: str) -> "Session":
        sess = Session(sid=sid, api=api, principal=principal, service=self)
        with self._lock:
            self._sessions[sess.id] = sess
        return sess


class Session:
    """固定快照的浏览会话；展开集合、选择与权限均绑定在会话上。"""

    def __init__(self, sid: str, api: str, principal: str, service: Service):
        self.id = "se_" + uuid.uuid4().hex[:12]
        self.snapshot_id = sid
        self.api = api
        self.principal = principal
        self.svc = service
        self.expanded: set[str] = set()
        self.selection: str | None = None

    def _resolver(self) -> Resolver:
        return self.svc.resolver(self.snapshot_id, self.api, self.principal)

    def _guard(self, node_id: str) -> dict[str, Any]:
        h = decode_handle(node_id)
        if h.get("snap") != self.snapshot_id:
            raise SnapshotMismatch(h.get("snap"), self.snapshot_id)
        return h

    def root(self) -> dict[str, Any]:
        node = self._resolver().root_tree()
        return {"snapshot_id": self.snapshot_id, "tree": _clean(node.to_dict())}

    def tree(self, node_id: str) -> dict[str, Any]:
        self._guard(node_id)
        node = self._resolver().shallow(node_id)
        return _clean(node.to_dict())

    def expand(self, node_id: str) -> dict[str, Any]:
        h = self._guard(node_id)
        path = h.get("path", "$")
        if not self._resolver().can_read(path):
            raise PermissionError(path)
        node = self._resolver().expand(node_id)
        self.expanded.add(node_id)
        return _clean(node.to_dict())

    def collapse(self, node_id: str) -> None:
        self._guard(node_id)
        self.expanded.discard(node_id)

    def select(self, path: str) -> None:
        self.selection = path

    # ---- 示例高亮：必须使用会话快照 ----
    def highlight(self, example_id: str) -> dict[str, Any]:
        ex = self.svc.store.get_example(example_id)
        result = ExampleValidator(self._resolver()).validate(ex["payload"])
        state_rec = self.svc.store.example_state(example_id, self.snapshot_id)
        # 按权限过滤路径，防止借示例泄露不可见字段
        filtered = {p: s for p, s in result["paths"].items()
                    if self._resolver().can_read(p)}
        issues = [i for i in result["issues"]
                  if self._resolver().can_read(i.get("path", "$"))]
        return {"example_id": example_id, "snapshot_id": self.snapshot_id,
                "recorded_state": (state_rec or {}).get("state", M.EX_PENDING),
                "live_state": result["state"], "paths": filtered, "issues": issues,
                "needs_revalidation": (state_rec or {}).get("state") in
                (M.EX_PENDING, M.EX_STALE, None)}

    # ---- 版本切换：展开态映射 ----
    def switch_version(self, version: str | None = None,
                       target_sid: str | None = None) -> dict[str, Any]:
        new_sid = target_sid or (
            self.svc.store.latest_snapshot(self.api) if version is None
            else self.svc.store.snapshot_for_version(self.api, version))
        if not new_sid:
            raise LookupError("目标版本不存在")
        if new_sid == self.snapshot_id:
            return {"snapshot_id": new_sid, "kept": [], "changed": [], "removed": []}

        old_fp = self.svc.store.param_fingerprints(self.snapshot_id)
        new_fp = self.svc.store.param_fingerprints(new_sid)
        old_res = self._resolver()

        kept, changed, removed = [], [], []
        new_expanded: set[str] = set()
        new_resolver = self.svc.resolver(new_sid, self.api, self.principal)
        for nid in self.expanded:
            try:
                h = decode_handle(nid)
            except Exception:
                continue
            path = h.get("path", "$")
            if path == "$":
                new_expanded.add(nid)
                continue
            if path not in new_fp:
                removed.append(path)
                continue
            nh = dict(h)
            nh["snap"] = new_sid
            # 结构位置可能变化；以新目录为准重建 loc 成本高，这里保留 loc，
            # 若句柄在新快照中无法解析，则按移除处理。
            new_id = encode_handle(nh)
            try:
                new_resolver.shallow(new_id)
            except Exception:
                removed.append(path)
                continue
            if not new_resolver.can_read(path):
                removed.append(path)
                continue
            new_expanded.add(new_id)
            (changed if old_fp.get(path) != new_fp.get(path) else kept).append(path)

        new_sel = None
        if self.selection:
            if self.selection in new_fp:
                new_sel = self.selection
            else:
                removed_sel = self.selection
                removed.append(removed_sel)

        self.snapshot_id = new_sid
        self.expanded = new_expanded
        self.selection = new_sel
        return {"snapshot_id": new_sid, "kept": sorted(kept),
                "changed": sorted(changed), "removed": sorted(set(removed))}

    def restore(self, expanded: list[str], selection: str | None) -> list[str]:
        """按分享内容恢复；返回因权限被剔除的节点。"""
        dropped = []
        valid = set()
        res = self._resolver()
        for nid in expanded:
            try:
                h = decode_handle(nid)
            except Exception:
                dropped.append(nid)
                continue
            if h.get("snap") != self.snapshot_id:
                dropped.append(nid)
                continue
            if not res.can_read(h.get("path", "$")):
                dropped.append(nid)
                continue
            valid.add(nid)
        self.expanded = valid
        if selection and self.svc.store.param(self.snapshot_id, selection):
            self.selection = selection
        return dropped

    # ---- 导出：同一快照的树 + 示例 + 元数据 ----
    def export(self) -> dict[str, Any]:
        res = self._resolver()
        root = res.root_tree()
        # 按展开集合物化
        for nid in list(self.expanded):
            try:
                h = decode_handle(nid)
                if h.get("snap") != self.snapshot_id:
                    continue
            except Exception:
                continue

        examples_out = []
        for e in self.svc.store.examples_for_api(self.api):
            ex = self.svc.store.get_example(e["example_id"])
            live = ExampleValidator(res).validate(ex["payload"])
            rec = self.svc.store.example_state(e["example_id"], self.snapshot_id)
            examples_out.append({
                "example_id": e["example_id"], "name": e["name"],
                "payload": ex["payload"],
                "recorded_state": (rec or {}).get("state", M.EX_PENDING),
                "live_state": live["state"],
                "paths": {p: s for p, s in live["paths"].items()
                          if res.can_read(p)},
                "issues": [i for i in live["issues"] if res.can_read(i.get("path", "$"))],
            })
        return {
            "format": "param-tree-export/1",
            "snapshot_id": self.snapshot_id, "api": self.api,
            "selection": self.selection,
            "expanded": sorted(self.expanded),
            "tree": _clean(root.to_dict()),
            "examples": examples_out,
            "versions": self.svc.store.versions(self.api),
        }


def _clean(obj: Any) -> Any:
    """去掉 to_dict 中 dataclass 冗余键，保持 JSON 简洁。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()
                if v is not None or k in ("default", "condition", "enum",
                                          "description", "ref_target", "value_type",
                                          "branch_label")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj
