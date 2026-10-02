"""验收测试：同名字段不同路径 / 条件分支冲突 / 递归对象 / 规范升级 /
展开过程中版本切换，以及默认-缺省-null、权限与分享。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ptsvc import model as M
from ptsvc.resolver import Resolver, SnapshotMismatch, decode_handle
from ptsvc.service import Service
from ptsvc.store import Store

ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "billing": {"type": "object", "properties": {
            "address": {"type": "object", "properties": {
                "zip": {"type": "string", "description": "账单邮编"}},
                "required": ["zip"]}}},
        "shipping": {"type": "object", "properties": {
            "address": {"type": "object", "properties": {
                "zip": {"type": "string", "description": "收货邮编"}}}}},
    },
    "required": ["billing"],
}

PERSON_COMPANY = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["person", "company"]},
        "ssn": {"type": "string"},
    },
    "required": ["kind"],
    "oneOf": [
        {"properties": {"kind": {"const": "person"}, "ssn": {"type": "string"}},
         "required": ["ssn"]},
        {"properties": {"kind": {"const": "company"},
                        "tax_id": {"type": "string"}}, "required": ["tax_id"]},
    ],
}

TREE_SCHEMA = {
    "$defs": {"Node": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
        },
        "required": ["id"]}},
    "$ref": "#/$defs/Node",
}

BAD_ALIAS_CYCLE = {
    "$defs": {
        "A": {"allOf": [{"$ref": "#/$defs/B"}],
              "properties": {"x": {"type": "integer"}}},
        "B": {"allOf": [{"$ref": "#/$defs/A"}],
              "properties": {"y": {"type": "string"}}},
    },
    "allOf": [{"$ref": "#/$defs/A"}],
}


def expand_path(r: Resolver, root, names):
    node = root
    for want in names:
        if not node.children and node.expandable:
            node = r.expand(node.node_id)
        match = next(c for c in node.children if c.name == want)
        node = r.expand(match.node_id) if match.expandable and not match.children else match
    return node


class Scenario1SameNameDifferentPath(unittest.TestCase):
    def test_same_name_different_paths(self):
        r = Resolver("sha256:s1", ADDRESS_SCHEMA)
        root = r.root_tree()
        bill = expand_path(r, root, ["billing", "address", "zip"])
        ship = expand_path(r, root, ["shipping", "address", "zip"])
        self.assertEqual(bill.param_path, "billing.address.zip")
        self.assertEqual(ship.param_path, "shipping.address.zip")
        self.assertNotEqual(bill.node_id, ship.node_id)
        self.assertEqual(bill.group(), "required")
        self.assertEqual(ship.group(), "optional")
        self.assertNotEqual(bill.description, ship.description)


class Scenario2ConditionalConflict(unittest.TestCase):
    def test_conditional_required_is_not_boolean(self):
        r = Resolver("sha256:s2", PERSON_COMPANY)
        root = r.root_tree()
        ssn = expand_path(r, root, ["ssn"])
        # 条件必填：谓词列表，而非全局布尔；分组为 conditional
        self.assertTrue(any(x.require == "required" and x.when for x in ssn.requiredness))
        self.assertEqual(ssn.group(), "conditional")
        tax = expand_path(r, root, ["tax_id"])
        self.assertEqual(tax.group(), "conditional")
        self.assertIn("company", [x.when for x in tax.requiredness if x.when][0])

    def test_branch_conflict_flagged(self):
        bad = {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "ssn": {"type": "string"}},
            "required": ["kind"],
            "if": {"properties": {"kind": {"const": "x"}}},
            "then": {"required": ["ssn"], "not": {"required": ["ssn"]}},
        }
        r = Resolver("sha256:s2b", bad)
        ssn = expand_path(r, root_of(r), ["ssn"])
        codes = {d.code for d in ssn.diagnostics}
        self.assertIn(M.BRANCH_CONFLICT, codes)


def root_of(r):
    return r.root_tree()


class Scenario3Recursion(unittest.TestCase):
    def test_legal_recursion_is_ref_node_with_depth_limit(self):
        r = Resolver("sha256:s3", TREE_SCHEMA)
        root = r.root_tree()
        # 根首次物化就是 Node 对象（初始进入不是回边）
        self.assertEqual(root.kind, M.OBJECT)
        item1 = expand_path(r, root, ["children", "[]（数组项）"])
        self.assertEqual(item1.kind, M.REF)
        self.assertTrue(item1.recursive)
        self.assertEqual(item1.ref_target, "#/$defs/Node")
        # 连续沿 children->[] 向下，回边均为引用节点，最多 3 层
        levels = 0
        node = item1
        for _ in range(6):
            children = expand_path(r, node, ["children"]) if node.expandable else None
            if children is None:
                break
            item = expand_path(r, node, ["children", "[]（数组项）"])
            levels += 1
            self.assertEqual(item.kind, M.REF)
            node = item
            if not node.expandable:
                break
        self.assertLessEqual(levels, M.MAX_REF_EXPAND)
        self.assertFalse(node.expandable)  # 深度耗尽：桩保留，非异常

    def test_error_cycle_is_reported_not_recursed(self):
        r = Resolver("sha256:s3b", BAD_ALIAS_CYCLE)
        root = r.root_tree()
        codes = {d.code for d in root.diagnostics}
        self.assertIn(M.REF_CYCLE, codes)
        # 错误环不会产生无限引用节点：正常属性仍渲染
        names = {c.name for c in root.children}
        self.assertEqual(names, {"x", "y"})

    def test_dangling_ref_is_error_node(self):
        r = Resolver("sha256:s3c",
                     {"type": "object",
                      "properties": {"a": {"$ref": "#/$defs/Missing"}}})
        a = expand_path(r, r.root_tree(), ["a"])
        self.assertEqual(a.kind, M.ERROR)
        self.assertEqual(a.diagnostics[0].code, M.REF_DANGLING)
        self.assertFalse(a.expandable)


class Scenario4SpecUpgrade(unittest.TestCase):
    def setUp(self):
        self.svc = Service(Store(":memory:"))

    def test_spec_upgrade_revalidation(self):
        v1 = {"type": "object",
              "properties": {"name": {"type": "string"},
                             "age": {"type": "integer"}},
              "required": ["name"]}
        pub1 = self.svc.publish("users", "1.0", v1)
        sid1 = pub1["snapshot_id"]
        # 两个示例
        good = self.svc.add_example("users", "good", {"name": "a", "age": 1})
        r1 = self.svc.store.example_state(good["example_id"], sid1)
        self.assertEqual(r1["state"], M.EX_PASSED)
        bad = self.svc.add_example("users", "no-name", {"age": 2})
        self.assertEqual(
            self.svc.store.example_state(bad["example_id"], sid1)["state"],
            M.EX_FAILED)

        # v2：email 新增并必填（影响所有对象载荷：含 name 路径指纹变化? 仅 email）
        v2 = {"type": "object",
              "properties": {"name": {"type": "string"},
                             "age": {"type": "integer"},
                             "email": {"type": "string"}},
              "required": ["name", "email"]}
        pub2 = self.svc.publish("users", "2.0", v2)
        sid2 = pub2["snapshot_id"]
        self.assertIn("email", pub2["changed_paths"])
        # 旧示例覆盖了对象字段（email 缺失但 name 路径指纹相同；email 是新增），
        # 两个示例的载荷均未包含 email → 反向索引中不触达 email，
        # 但 name 仍是旧指纹。这里通过 changed_paths 与“整对象根”语义，
        # 未触达示例应立即重跑，结果应当 failed（缺 email）。
        # 至少：新快照下两示例均不得显示 passed/“已适配”。
        states = {
            self.svc.store.example_state(good["example_id"], sid2)["state"],
            self.svc.store.example_state(bad["example_id"], sid2)["state"],
        }
        self.assertNotIn(M.EX_PASSED, states)

    def test_touched_example_enters_pending_queue(self):
        v1 = {"type": "object",
              "properties": {"name": {"type": "string"},
                             "age": {"type": "integer", "default": 0}},
              "required": ["name"]}
        pub1 = self.svc.publish("users", "1.0", v1)
        sid1 = pub1["snapshot_id"]
        ex = self.svc.add_example("users", "ex", {"name": "a", "age": 5})["example_id"]
        # 显式让示例覆盖 age 路径（payload 有 age）
        # v2 修改 age 类型 → age 指纹变化 → 示例触达 → pending
        v2 = {"type": "object",
              "properties": {"name": {"type": "string"},
                             "age": {"type": "string", "default": "0"}},
              "required": ["name"]}
        pub2 = self.svc.publish("users", "2.0", v2)
        sid2 = pub2["snapshot_id"]
        self.assertIn("age", pub2["changed_paths"])
        self.assertEqual(
            self.svc.store.example_state(ex, sid2)["state"], M.EX_PENDING)
        self.assertIn(ex, [q["example_id"] for q in self.svc.store.queue(sid2)])
        # 复验：age 给的是整数 → 失败；旧状态不再显示 pending
        res = self.svc.revalidate(ex, sid2)
        self.assertEqual(res["state"], M.EX_FAILED)
        self.assertEqual(
            self.svc.store.example_state(ex, sid2)["state"], M.EX_FAILED)
        # 旧快照状态保持原样
        self.assertEqual(
            self.svc.store.example_state(ex, sid1)["state"], M.EX_PASSED)


class Scenario5VersionSwitch(unittest.TestCase):
    def setUp(self):
        self.svc = Service(Store(":memory:"))
        self.v1 = {"type": "object", "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"}}, "required": ["name"]}
        self.sid1 = self.svc.publish("u", "1.0", self.v1)["snapshot_id"]
        self.sess = self.svc.create_session("u", "alice")

    def test_expand_then_switch_maps_state(self):
        root = self.sess.root()["tree"]
        name_node = next(c for c in root["children"] if c["name"] == "name")
        age_node = next(c for c in root["children"] if c["name"] == "age")
        self.sess.expand(name_node["node_id"])
        self.sess.expand(age_node["node_id"])
        self.assertEqual(len(self.sess.expanded), 2)

        v2 = {"type": "object", "properties": {
            # name 保留；age 类型变化（changed）；新增 email；删除无
            "name": {"type": "string"},
            "age": {"type": "string"},
            "email": {"type": "string"}}, "required": ["name"]}
        sid2 = self.svc.publish("u", "2.0", v2)["snapshot_id"]
        result = self.sess.switch_version("2.0")
        self.assertEqual(result["snapshot_id"], sid2)
        self.assertIn("age", result["changed"])
        self.assertIn("name", result["kept"])
        # 旧句柄立即失效
        with self.assertRaises(SnapshotMismatch):
            self.sess.expand(name_node["node_id"])
        # 展开集合已经迁移为新句柄
        for nid in self.sess.expanded:
            self.assertEqual(decode_handle(nid)["snap"], sid2)

    def test_switch_removed_path_is_dropped(self):
        root = self.sess.root()["tree"]
        age = next(c for c in root["children"] if c["name"] == "age")
        self.sess.expand(age["node_id"])
        v2 = {"type": "object",
              "properties": {"name": {"type": "string"}}, "required": ["name"]}
        self.svc.publish("u", "2.0", v2)
        result = self.sess.switch_version("2.0")
        self.assertIn("age", result["removed"])
        self.assertEqual(len(self.sess.expanded), 0)

    def test_tree_highlight_export_use_same_snapshot(self):
        # v1 示例合法；v2 增加必填 email
        ex = self.svc.add_example("u", "ex", {"name": "a", "age": 1})["example_id"]
        hl1 = self.sess.highlight(ex)
        self.assertEqual(hl1["snapshot_id"], self.sid1)
        self.assertEqual(hl1["recorded_state"], M.EX_PASSED)
        v2 = {"type": "object", "properties": {
            "name": {"type": "string"}, "age": {"type": "integer"},
            "email": {"type": "string"}}, "required": ["name", "email"]}
        sid2 = self.svc.publish("u", "2.0", v2)["snapshot_id"]
        self.sess.switch_version("2.0")
        hl2 = self.sess.highlight(ex)
        # 高亮与会话同一快照，且示例在新快照下不是 passed
        self.assertEqual(hl2["snapshot_id"], sid2)
        self.assertNotEqual(hl2["recorded_state"], M.EX_PASSED)
        bundle = self.sess.export()
        self.assertEqual(bundle["snapshot_id"], sid2)
        self.assertEqual(bundle["tree"]["node_id"].split(".")[0][:2], "n_")
        # 导出包内示例状态同样来自 sid2
        exrec = bundle["examples"][0]
        self.assertNotEqual(exrec["recorded_state"], M.EX_PASSED)


class DefaultAbsentNull(unittest.TestCase):
    def test_three_way_distinction(self):
        schema = {"type": "object", "properties": {
            "with_default": {"type": "integer", "default": 0},
            "no_default": {"type": "string"},
            "nullable": {"type": ["string", "null"]}},
            "required": ["nullable"]}
        r = Resolver("sha256:dn", schema)
        from ptsvc.validator import ExampleValidator
        res = ExampleValidator(r).validate({"nullable": None})
        self.assertEqual(res["paths"]["with_default"], M.ST_DEFAULTED)
        self.assertEqual(res["paths"]["no_default"], M.ST_ABSENT)
        self.assertEqual(res["paths"]["nullable"], M.ST_NULL)
        # null 用于不允许 null 字段报错
        res2 = ExampleValidator(r).validate({"nullable": "x", "no_default": None})
        self.assertIn("no_default", [i["path"] for i in res2["issues"]])
        # 节点模型：default 与 nullable 独立
        root = r.root_tree()
        wd = next(c for c in root.children if c.name == "with_default")
        self.assertTrue(wd.has_default and wd.default == 0)
        nb = next(c for c in root.children if c.name == "nullable")
        self.assertTrue(nb.nullable and not nb.has_default)


class PermissionAndShare(unittest.TestCase):
    def setUp(self):
        self.svc = Service(Store(":memory:"))
        self.schema = {"type": "object", "properties": {
            "public": {"type": "string"},
            "secret": {"type": "object", "properties": {
                "token": {"type": "string"}}}}}
        self.sid = self.svc.publish("api", "1.0", self.schema)["snapshot_id"]
        self.svc.store.set_policy("api", "secret*", "bob", "deny")

    def test_subtree_hidden_even_in_example_highlight(self):
        sess = self.svc.create_session("api", "bob")
        root = sess.root()["tree"]
        names = {c["name"]: c for c in root["children"]}
        self.assertEqual(names["secret"]["kind"], M.ERROR)
        self.assertEqual(names["secret"]["diagnostics"][0]["code"],
                         M.FORBIDDEN_NODE)
        ex = self.svc.add_example("api", "ex",
                                  {"public": "p", "secret": {"token": "t"}})["example_id"]
        hl = sess.highlight(ex)
        self.assertIn("public", hl["paths"])
        self.assertNotIn("secret.token", hl["paths"])  # 不可借示例泄露
        with self.assertRaises(PermissionError):
            sess.expand(names["secret"]["node_id"])

    def test_share_does_not_transfer_permissions(self):
        owner = self.svc.create_session("api", "alice")
        root = owner.root()["tree"]
        secret = next(c for c in root["children"] if c["name"] == "secret")
        owner.expand(secret["node_id"])
        share_id = self.svc.share(owner)
        # bob 重建：secret 展开被剔除
        rec = self.svc.from_share(share_id, "bob")
        sess = self.svc.get_session(rec["session_id"])
        self.assertTrue(rec["dropped"])
        self.assertEqual(len(sess.expanded), 0)
        # 快照仍是分享时的快照
        self.assertEqual(rec["snapshot_id"], self.sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
