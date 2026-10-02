"""HTTP 端到端：服务启动、快照同源（tree/examples/export 共指 version_id）、
版本切换、分享单版本授权、惰性与全量结果一致、缓存命中结果稳定。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema_service import db as dbmod
from schema_service.server import make_handler

SPECS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'specs')
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')


def load(n):
    with open(os.path.join(SPECS, n), encoding='utf-8') as f:
        return json.load(f)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dbp = os.path.join(cls.tmp.name, 't.db')
        conn = dbmod.connect(cls.dbp); dbmod.init_db(conn); conn.close()
        handler = make_handler(cls.dbp, STATIC)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.tmp.cleanup()

    def req(self, method, path, body=None, user='alice'):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={'Content-Type': 'application/json',
                                            'X-User-Id': user})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def publish(self, ver, spec):
        return self.req('POST', f'/api/specs/order/versions',
                        {'version': ver, 'spec': spec})[1]

    def test_full_flow_and_snapshot_consistency(self):
        v1 = self.publish('1.0.0', load('order-v1.json'))['id']
        # 表格数据（惰性）
        st, lazy = self.req('GET', f'/api/versions/{v1}/children?root=OrderCreate&id=')
        self.assertEqual(st, 200)
        self.assertEqual(lazy['node']['name'], 'OrderCreate')
        # 全量
        st, full = self.req('GET', f'/api/versions/{v1}/tree?root=OrderCreate')
        self.assertEqual(st, 200)
        # 缓存再次取惰性，结果一致
        _, lazy2 = self.req('GET', f'/api/versions/{v1}/children?root=OrderCreate&id=')
        self.assertEqual(lazy, lazy2)

        # 提交合法/非法示例
        st, ok = self.req('POST', f'/api/versions/{v1}/examples', {
            'root': 'PaymentOrder', 'title': 'p',
            'payload': {'kind': 'balance', 'balancePwd': '1'}})
        self.assertEqual(st, 201); self.assertTrue(ok['valid'])
        st, bad = self.req('POST', f'/api/versions/{v1}/examples', {
            'root': 'OrderCreate', 'title': 'bad', 'payload': {'items': []}})
        self.assertFalse(bad['valid'])

        # 发布 v2 并复验
        v2 = self.publish('2.0.0', load('order-v2.json'))['id']
        st, counts = self.req('POST', f'/api/versions/{v2}/revalidate', {})
        self.assertEqual(st, 200)

        # 关键：tree / examples / export 三个消费者锚定同一快照 id
        _, ex2 = self.req('GET', f'/api/versions/{v2}/examples?root=PaymentOrder')
        _, exp2 = self.req('GET', f'/api/versions/{v2}/export?root=OrderCreate')
        self.assertEqual(exp2['version_id'], v2)
        # PaymentOrder 示例在 v2 未受影响：仍是 valid，basis 仍为 v1（非 v2！）
        pay = ex2['examples'][0]
        self.assertEqual(pay['status'], 'valid')
        self.assertEqual(pay['basis_version_id'], v1)
        self.assertEqual(pay['current_version_id'], v2)

        # 导出的树与接口树是同一份快照构建
        self.assertEqual(len(exp2['trees']['OrderCreate']['children']),
                         len(full['node']['children']))

    def test_acl_and_share_scope(self):
        v1 = self.publish('1.0.0', load('order-v1.json'))['id']
        # 匿名无权限
        st, _ = self.req('GET', f'/api/versions/{v1}/roots', user='stranger')
        self.assertEqual(st, 403)
        # 分享令牌
        st, sh = self.req('POST', '/api/share', {'vid': v1, 'expanded': []})
        self.assertEqual(st, 201)
        token = sh['token']
        st2, _ = self.req('GET', f'/api/versions/{v1}/roots?share={token}', user='x')
        self.assertEqual(st2, 200)

    def test_switch_version_mid_expansion(self):
        v1 = self.publish('1.0.0', load('order-v1.json'))['id']
        v2 = self.publish('2.0.0', load('order-v2.json'))['id']
        # v1 里的节点 id 在 v2 已删除 -> 404，而不是返回 v1 数据
        st, body = self.req('GET',
            f'/api/versions/{v2}/children?root=OrderCreate&id=p~cardNo')
        self.assertEqual(st, 404)
        st, body = self.req('GET',
            f'/api/versions/{v1}/children?root=OrderCreate&id=p~cardNo')
        self.assertEqual(st, 200)
        # 两个版本 sha 不同，快照不可变
        _, m1 = self.req('GET', f'/api/versions/{v1}')
        _, m2 = self.req('GET', f'/api/versions/{v2}')
        self.assertNotEqual(m1['sha256'], m2['sha256'])

    def test_csv_export_and_static_page(self):
        v1 = self.publish('1.0.0', load('order-v1.json'))['id']
        r = urllib.request.Request(
            self.base + f'/api/versions/{v1}/export?root=OrderCreate&fmt=csv',
            headers={'X-User-Id': 'alice'})
        with urllib.request.urlopen(r) as resp:
            csv_text = resp.read().decode()
        self.assertIn('conditional', csv_text)
        self.assertIn('OrderCreate', csv_text)
        with urllib.request.urlopen(self.base + '/') as resp:
            self.assertIn('参数树', resp.read().decode())


if __name__ == '__main__':
    unittest.main(verbosity=2)
