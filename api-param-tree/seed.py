"""初始化演示库：发布 order 规范 v1 -> 录入示例 -> 发布 v2（触发复验标记）。

用法：python -m seed [重置则删除 data/app.db]
"""
from __future__ import annotations

import json
import os

from schema_service import db as dbmod
from schema_service import examples as ex_mod
from schema_service import shares, versions

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'data', 'app.db')


def load(name):
    with open(os.path.join(BASE, 'specs', name), encoding='utf-8') as f:
        return json.load(f)


def main(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = dbmod.connect(DB_PATH)
    dbmod.init_db(conn)

    # ---- v1（首个版本的创建者成为 admin，须在 create_version 提交前授权）----
    shares.grant(conn, 'alice', 'order', 'admin')
    shares.grant(conn, 'bob', 'order', 'reader')
    v1, _, _ = versions.create_version(
        conn, 'order', '1.0.0', load('order-v1.json'), 'alice')

    # 合法示例：银行卡支付 + 递归子项
    ex_mod.add_example(conn, v1, 'OrderCreate', '银行卡下单', {
        'payType': 'card',
        'cardNo': '6222 0000 1111 2222',
        'contact': '张三',
        'couponCode': 'SAVE10',
        'address': {'city': '上海', 'detail': '南京东路 1 号', 'note': '工作日送达'},
        'items': [
            {'sku': 'SKU-1', 'qty': 2, 'note': '加急',
             'children': [{'sku': 'SKU-1-SUB', 'qty': 1}]},
        ],
    }, 'alice')

    # 货到付款示例：v1 下 contact/address 只是条件必填，此例满足；
    # v2 起 contact 升为全局必填（本例行仍满足），address 规则不变。
    ex_mod.add_example(conn, v1, 'OrderCreate', '货到付款（v1 合法，v2 复验）', {
        'payType': 'cod',
        'contact': '李四',
        'address': {'city': '广州'},
        'items': [{'sku': 'SKU-2', 'qty': 1}],
    }, 'bob')

    # 含显式 null 与缺省默认的示例（qty 缺省取默认 1，v1 合法）
    ex_mod.add_example(conn, v1, 'OrderCreate', '发票下单（null/缺省/默认三态）', {
        'payType': 'invoice',
        'invoiceTitle': None,
        'address': {'city': '北京'},
        'items': [{'sku': 'SKU-3'}],
    }, 'bob')

    # 分支示例（oneOf）
    ex_mod.add_example(conn, v1, 'PaymentOrder', '余额支付分支', {
        'kind': 'balance', 'balancePwd': 'pw-123',
    }, 'alice')

    # 发票自提：v1 合法（invoiceTitle 条件必填已给，contact 仅 cod 必填）；
    # v2 中 contact 升为全局必填 -> 复验应判 invalid，且 basis 不能停留在 v1。
    ex_mod.add_example(conn, v1, 'OrderCreate', '发票自提（v1 合法，v2 缺 contact）', {
        'payType': 'invoice',
        'invoiceTitle': '某公司',
        'address': {'city': '深圳'},
        'items': [{'sku': 'SKU-4', 'qty': 3}],
    }, 'carol')

    # 故意非法的示例（缺 payType 等），复验后应稳定 invalid
    ex_mod.add_example(conn, v1, 'OrderCreate', '非法示例（缺必填）', {
        'items': [],
    }, 'charlie')

    # ---- v2：参数变更，依赖示例自动 needs_revalidation ----
    v2, diff, affected = versions.create_version(
        conn, 'order', '2.0.0', load('order-v2.json'), 'alice')
    print('v1 =', v1, ' v2 =', v2)
    print('diff =', json.dumps(diff, ensure_ascii=False, indent=2))
    print('affected examples =', affected)

    counts = versions.revalidate(conn, v2)
    print('revalidate counts =', counts)

    conn.close()
    return v1, v2


if __name__ == '__main__':
    import sys
    main(reset='--reset' in sys.argv)
