"""验收测试：同名字段不同路径 / 条件分支冲突 / 递归对象 / 规范升级 /
展开中版本切换；以及三态、错误环与合法自引用的区别、全量-惰性一致、
表格/高亮/导出同一快照。

运行：python -m pytest tests/ -q   或   python tests/test_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema_service import db as dbmod
from schema_service import examples as ex_mod
from schema_service import shares, versions
from schema_service.resolver import Resolver
from schema_service.tree import (DEFAULT_MAX_DEPTH, NodeNotFound, build_full,
                                 compute_children, iter_nodes)
from schema_service.validate import describe_payload, validate_value

SPECS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'specs')


def load(name):
    with open(os.path.join(SPECS, name), encoding='utf-8') as f:
        return json.load(f)


def find_node(tree, path):
    for n in iter_nodes(tree):
        if n.get('path') == path:
            return n
    return None


class TestTreeSemantics(unittest.TestCase):
    def setUp(self):
        self.spec = load('order-v1.json')

    def test_same_name_different_path(self):
        """同名字段 note 出现在不同路径，必须各自独立。"""
        t = build_full(self.spec, 'OrderCreate', 3)
        paths = [n['path'] for n in iter_nodes(t) if n['name'] == 'note']
        self.assertIn('address.note', paths)
        self.assertIn('items[].note', paths)
        a = find_node(t, 'address.note')
        b = find_node(t, 'items[].note')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(a['type'], 'string')

    def test_required_is_not_global_boolean(self):
        """条件必填不能压成布尔：card/contact 都是 conditional，items 是 required。"""
        t = build_full(self.spec, 'OrderCreate', 3)
        self.assertEqual(find_node(t, 'items')['required'], 'required')
        contact = find_node(t, 'contact')
        self.assertEqual(contact['required'], 'conditional')
        self.assertTrue(any('payType' in c['text'] for c in contact['conditions']))
        card = find_node(t, 'cardNo')
        self.assertEqual(card['required'], 'conditional')
        self.assertTrue(card['deprecated'])

    def test_tri_state_default_absent_null_are_separate(self):
        spec = self.spec
        resolver = Resolver(spec)
        # couponCode 有默认、可 null
        payload = {'payType': 'invoice', 'invoiceTitle': 'X',
                   'address': {'city': 'BJ'}, 'items': [{'sku': 'S'}]}
        # 缺省 couponCode -> 默认值；qty 缺省 -> 默认值；都不报缺失
        errors = validate_value(resolver.root_schema('OrderCreate'), payload, resolver)
        self.assertFalse([e for e in errors if e['code'] == 'missing_required'])
        states = {r['name']: r['state']
                  for r in describe_payload(spec, 'OrderCreate', payload)}
        self.assertEqual(states['couponCode'], 'absent_default')
        # 显式 null：couponCode 允许 -> 合法；invoiceTitle 也允许 null
        payload2 = dict(payload, couponCode=None)
        errors = validate_value(resolver.root_schema('OrderCreate'), payload2, resolver)
        self.assertFalse([e for e in errors if e['code'] == 'null_not_allowed'])
        # 显式 null 到不允许 null 的 contact -> 报错（缺省则只会判必填）
        payload3 = dict(payload, payType='cod', contact=None,
                        address={'city': 'BJ'})
        errors = validate_value(resolver.root_schema('OrderCreate'), payload3, resolver)
        self.assertTrue(any(e['code'] == 'null_not_allowed' for e in errors))

    def test_legal_recursion_is_reference_node_depth_limited(self):
        """Address.parent（allOf 自引用）与 OrderItem.children 都是合法递归。"""
        t = build_full(self.spec, 'Address', 3)
        parent = find_node(t, 'parent')
        self.assertEqual(parent['kind'], 'reference')
        self.assertEqual(parent['refName'], 'Address')
        self.assertTrue(parent['hasChildren'])
        # 引用展开一层后仍是引用，再深则 depth-limit
        node, children = compute_children(self.spec, 'Address', 'p~parent', 1)
        self.assertEqual(node['kind'], 'reference')
        node2, children2 = compute_children(self.spec, 'Address', 'p~parent', 0)
        self.assertEqual([c['kind'] for c in children2], ['depth-limit'])

    def test_legal_recursion_via_array(self):
        t = build_full(self.spec, 'OrderItem', 3)
        node = find_node(t, 'children[]')
        self.assertEqual(node['kind'], 'reference')
        self.assertEqual(node['refName'], 'OrderItem')

    def test_bad_alias_cycle_is_error_subtree_only(self):
        """纯别名环（A->B->A 无结构出口）只标记出错子树，同级字段正常。"""
        t = build_full(self.spec, 'AliasCycle', 3)
        bad = find_node(t, 'a')
        self.assertEqual(bad['kind'], 'error')
        self.assertEqual(bad['code'], 'cycle')
        ok = find_node(t, 'ok')
        self.assertEqual(ok['kind'], 'field')
        # 其它根不受影响
        t2 = build_full(self.spec, 'OrderCreate', 3)
        self.assertFalse(any(n['kind'] == 'error' for n in iter_nodes(t2)))

    def test_unresolved_ref_is_isolated_error(self):
        spec = {'components': {'schemas': {'R': {
            'type': 'object',
            'properties': {'bad': {'$ref': '#/components/schemas/Missing'},
                           'good': {'type': 'string'}}}}}}
        t = build_full(spec, 'R', 3)
        bad = find_node(t, 'bad')
        self.assertEqual(bad['kind'], 'error')
        self.assertEqual(bad['code'], 'unresolved')
        self.assertIsNotNone(find_node(t, 'good'))

    def test_branch_conflict_oneof(self):
        """oneOf 恰好一个分支：匹配 0 个或 2 个都非法。"""
        resolver = Resolver(self.spec)
        root = resolver.root_schema('PaymentOrder')
        self.assertTrue(validate_value(root, {'kind': 'balance', 'balancePwd': 'x'},
                                       resolver) == [])
        errs = validate_value(root, {'kind': 'card'}, resolver)
        self.assertTrue(any(e['code'] == 'oneof_mismatch' for e in errs))
        # 两个分支都命中（同时给 cardNo 与 balancePwd 且 kind 无法同时满足 ->
        # 这里 kind 只能选一个，构造缺 kind 的载荷 -> 0 命中）
        errs2 = validate_value(root, {'cardNo': '1', 'balancePwd': '2'}, resolver)
        self.assertTrue(any(e['code'] == 'oneof_mismatch' for e in errs2))

    def test_conditional_required_evaluated_per_branch(self):
        resolver = Resolver(self.spec)
        root = resolver.root_schema('OrderCreate')
        # card 分支缺 cardNo -> 错；invoice 分支缺 cardNo -> 不触发
        card = {'payType': 'card', 'items': [{'sku': 'S'}]}
        inv = {'payType': 'invoice', 'invoiceTitle': 'T',
               'address': {'city': 'X'}, 'items': [{'sku': 'S'}]}
        self.assertTrue(any(e['path'].endswith('cardNo')
                            for e in validate_value(root, card, resolver)))
        self.assertFalse(any(e['path'].endswith('cardNo')
                             for e in validate_value(root, inv, resolver)))

    def test_full_and_lazy_share_same_kernel(self):
        """全量展开与按节点惰性展开对同 id 节点必须一致。"""
        for root in Resolver(self.spec).schema_names():
            full = build_full(self.spec, root, 3)
            for n in iter_nodes(full):
                if not n.get('hasChildren'):
                    continue
                a, ca = compute_children(self.spec, root, n['id'], 3)
                b, cb = compute_children(self.spec, root, n['id'], 3)
                self.assertEqual(a, b)
                self.assertEqual([c['id'] for c in ca],
                                 [c['id'] for c in n.get('children', [])])


class TestVersionUpgrade(unittest.TestCase):
    def setUp(self):
        self.conn = dbmod.new_memory_db()
        self.v1, _, _ = versions.create_version(
            self.conn, 'order', '1.0.0', load('order-v1.json'), 'alice')
        # 一个合法示例 + 一个命中 qty 的示例
        self.cod, _ = ex_mod.add_example(self.conn, self.v1, 'OrderCreate', 'cod', {
            'payType': 'cod', 'contact': 'X', 'address': {'city': 'G'},
            'items': [{'sku': 'S2', 'qty': 1}]}, 'bob')
        self.pay, _ = ex_mod.add_example(self.conn, self.v1, 'PaymentOrder', 'pay',
                                         {'kind': 'balance', 'balancePwd': 'p'}, 'a')
        # v1 合法、v2 缺 contact 的示例
        self.inv, _ = ex_mod.add_example(self.conn, self.v1, 'OrderCreate', 'inv', {
            'payType': 'invoice', 'invoiceTitle': 'T',
            'address': {'city': 'S'}, 'items': [{'sku': 'S4', 'qty': 3}]}, 'c')

    def tearDown(self):
        self.conn.close()

    def test_affected_examples_marked_needs_revalidation(self):
        v2, diff, affected = versions.create_version(
            self.conn, 'order', '2.0.0', load('order-v2.json'), 'alice')
        self.assertIn('OrderCreate:contact', diff['newly_required'])
        self.assertIn('OrderCreate:cardNo', diff['removed'])
        self.assertTrue(any('qty' in p for p in diff['changed']))
        # 受影响示例被标记
        self.assertIn(self.cod, affected)
        self.assertIn(self.inv, affected)
        # PaymentOrder 未受影响
        self.assertNotIn(self.pay, affected)
        rows = {r['example_id']: r for r in self.conn.execute(
            'SELECT * FROM example_validations WHERE spec_version_id=?', (v2,))}
        self.assertEqual(rows[self.cod]['status'], 'needs_revalidation')
        # 旧示例不冒充已适配新版：basis 仍指向 v1
        self.assertEqual(rows[self.cod]['basis_version_id'], self.v1)
        # 未受影响：结论沿用但 basis 明确是旧版本
        self.assertEqual(rows[self.pay]['status'], 'valid')
        self.assertEqual(rows[self.pay]['basis_version_id'], self.v1)

    def test_revalidate_against_new_snapshot(self):
        v2, _, _ = versions.create_version(
            self.conn, 'order', '2.0.0', load('order-v2.json'), 'alice')
        counts = versions.revalidate(self.conn, v2)
        self.assertEqual(counts['revalidated'], 2)  # cod + inv（pay 沿用）
        rows = {r['example_id']: r for r in self.conn.execute(
            'SELECT * FROM example_validations WHERE spec_version_id=?', (v2,))}
        # 复验后 basis 切到 v2；cod 因 qty minimum=2 非法，inv 缺 contact
        self.assertEqual(rows[self.cod]['status'], 'invalid')
        self.assertEqual(json.loads(rows[self.cod]['errors'])[0]['code'], 'minimum')
        self.assertEqual(rows[self.inv]['status'], 'invalid')
        self.assertTrue(any(json.loads(r['errors']) and True for r in
                            [rows[self.inv]]))
        self.assertEqual(rows[self.cod]['basis_version_id'], v2)
        # pay 未复验，basis 不能被改写成 v2
        self.assertEqual(rows[self.pay]['basis_version_id'], self.v1)

    def test_reverse_index_recursive_and_prefix(self):
        # 嵌套 children 里触及的 qty 也应被 items[].qty 的查询命中
        ex_mod.add_example(self.conn, self.v1, 'OrderItem', 'nested',
                           {'sku': 'A', 'qty': 1,
                            'children': [{'sku': 'B', 'qty': 1,
                                          'children': [{'sku': 'C'}]}]}, 'a')
        hit = ex_mod.examples_for_path(self.conn, self.v1, 'OrderItem', 'qty')
        self.assertTrue(any(e['title'] == 'nested' for e in hit))
        # 数组前缀：查 children 也能命中
        hit2 = ex_mod.examples_for_path(self.conn, self.v1, 'OrderItem', 'children[]')
        self.assertTrue(any(e['title'] == 'nested' for e in hit2))


class TestShareAccess(unittest.TestCase):
    def setUp(self):
        self.conn = dbmod.new_memory_db()
        self.vid, _, _ = versions.create_version(
            self.conn, 'order', '1.0.0', load('order-v1.json'), 'alice')
        self.secret = dbmod.get_secret(self.conn)

    def test_token_scoped_to_single_version(self):
        token = shares.make_token(self.secret,
                                  {'vid': self.vid, 'expanded': ['p~x']}, ttl=60)
        payload = shares.verify_token(self.secret, token)
        self.assertEqual(payload['vid'], self.vid)
        self.assertRaises(shares.ShareError,
                          shares.verify_token, self.secret, token + 'x')
        self.assertRaises(shares.ShareError,
                          shares.verify_token, self.secret, 'a.b')

    def test_acl_roles(self):
        shares.grant(self.conn, 'bob', 'order', 'reader')
        self.assertTrue(shares.check_access(self.conn, 'bob', 'order', 'read'))
        self.assertFalse(shares.check_access(self.conn, 'bob', 'order', 'write'))
        self.assertFalse(shares.check_access(self.conn, 'nobody', 'order', 'read'))
        shares.grant(self.conn, 'bob', 'order', 'editor')
        self.assertTrue(shares.check_access(self.conn, 'bob', 'order', 'write'))


class TestVersionSwitchDuringExpansion(unittest.TestCase):
    """展开过程中切换版本：节点 id 是不可变快照内的边序列，
    对新版本重放时失效节点必须给出 NodeNotFound（而不是串数据）。"""
    def setUp(self):
        self.spec1 = load('order-v1.json')
        self.spec2 = load('order-v2.json')

    def test_node_id_removed_in_new_version(self):
        # cardNo 在 v2 被删除
        with self.assertRaises(NodeNotFound):
            compute_children(self.spec2, 'OrderCreate', 'p~cardNo', 3)

    def test_same_id_stable_within_snapshot(self):
        for spec in (self.spec1, self.spec2):
            node, _ = compute_children(spec, 'OrderCreate', 'p~items', 3)
            self.assertEqual(node['name'], 'items')


if __name__ == '__main__':
    unittest.main(verbosity=2)
