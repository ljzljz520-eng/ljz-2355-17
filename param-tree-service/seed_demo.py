"""写入演示数据：users API 两个版本（含条件必填、递归、默认值）。"""
import sys
from ptsvc.server import make_server

v1 = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "用户名"},
        "age": {"type": "integer", "default": 18, "description": "年龄，缺省取默认 18"},
        "kind": {"type": "string", "enum": ["person", "company"]},
        "ssn": {"type": "string", "deprecated": False},
    },
    "required": ["name", "kind"],
    "oneOf": [
        {"properties": {"kind": {"const": "person"}, "ssn": {"type": "string"}},
         "required": ["ssn"]},
        {"properties": {"kind": {"const": "company"},
                        "tax_id": {"type": "string"}}, "required": ["tax_id"]},
    ],
}

v2 = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "用户名"},
        "age": {"type": "integer", "default": 18},
        "kind": {"type": "string", "enum": ["person", "company"]},
        "ssn": {"type": "string", "deprecated": True,
                "description": "已废弃，自然人请用 id_number"},
        "id_number": {"type": "string"},
        "email": {"type": "string", "description": "v2 新增必填"},
    },
    "required": ["name", "kind", "email"],
    "oneOf": [
        {"properties": {"kind": {"const": "person"},
                        "id_number": {"type": "string"}},
         "required": ["id_number"]},
        {"properties": {"kind": {"const": "company"},
                        "tax_id": {"type": "string"}}, "required": ["tax_id"]},
    ],
}

TREE = {
    "$defs": {"Node": {"type": "object", "properties": {
        "id": {"type": "string"},
        "label": {"type": "string", "default": "node"},
        "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
    }, "required": ["id"]}},
    "$ref": "#/$defs/Node",
}


def seed(db_path=":memory:", start=True):
    srv, svc = (None, None)
    from ptsvc.store import Store
    from ptsvc.service import Service
    store = Store(db_path)
    svc = Service(store)
    p1 = svc.publish("users", "1.0", v1, by="demo")
    svc.add_example("users", "person-ok",
                    {"name": "张三", "kind": "person", "ssn": "110xxx", "age": 30})
    svc.add_example("users", "company-missing-tax",
                    {"name": "某公司", "kind": "company"})
    svc.add_example("users", "default-and-null",
                    {"name": "李四", "kind": "person", "ssn": "x", "nickname": None})
    p2 = svc.publish("users", "2.0", v2, by="demo")
    svc.publish("tree", "1.0", TREE, by="demo")
    store.set_policy("users", "ssn*", "auditor", "deny")
    store.set_policy("users", "tax_id*", "auditor", "deny")
    print("v1:", p1["snapshot_id"], "| v2:", p2["snapshot_id"])
    print("v2 发布影响:", {
        "changed": p2["changed_paths"],
        "pending_examples": p2["revalidation_enqueued"],
        "untouched": p2["untouched_rechecked"]})
    if start:
        from ptsvc.server import make_server
        server, _ = make_server.__wrapped__ if hasattr(make_server, "__wrapped__") else (None, None)
    return store, svc


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    store, svc = seed(db, start=False)
    print("已写入", db)
