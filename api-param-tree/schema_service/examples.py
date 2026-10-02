"""示例管理与「参数 -> 示例」反向索引。"""
from __future__ import annotations

import json

from .resolver import Resolver
from .validate import describe_payload, validate_value


def touched_paths(data):
    """示例载荷触及的参数路径（含中间对象；数组元素记 []）。"""
    paths = set()

    def rec(value, p):
        if isinstance(value, dict):
            if p:
                paths.add(p)
            for k, v in value.items():
                rec(v, f'{p}.{k}' if p else k)
        elif isinstance(value, list):
            if p:
                paths.add(p)
            for item in value:
                rec(item, p + '[]')
        else:
            if p:
                paths.add(p)

    rec(data, '')
    return paths


def paths_related(p, q):
    """路径相关：互为前缀（含 [] 边界）。用于反向索引命中与 diff 影响面。"""
    return (p == q
            or p.startswith(q + '.')
            or q.startswith(p + '.')
            or p.startswith(q + '[')
            or q.startswith(p + '['))


def rebuild_index(conn, spec_version_id, example_id, root, payload):
    conn.execute(
        'DELETE FROM param_example_index WHERE spec_version_id=? AND example_id=?',
        (spec_version_id, example_id),
    )
    for p in sorted(touched_paths(payload)):
        conn.execute(
            'INSERT OR IGNORE INTO param_example_index '
            '(spec_version_id, param_path, example_id) VALUES (?,?,?)',
            (spec_version_id, f'{root}:{p}', example_id),
        )


def add_example(conn, spec_version_id, root, title, payload, user='anonymous'):
    """新增示例：立即按当前版本校验、写校验行与反向索引。"""
    ver = conn.execute('SELECT * FROM spec_versions WHERE id=?',
                       (spec_version_id,)).fetchone()
    if not ver:
        raise KeyError('版本不存在')
    spec = json.loads(ver['raw'])
    errors = validate_value(Resolver(spec).root_schema(root), payload,
                            Resolver(spec))
    cur = conn.execute(
        'INSERT INTO examples (spec_name, root, title, payload, created_by) '
        'VALUES (?,?,?,?,?)',
        (ver['spec_name'], root, title,
         json.dumps(payload, ensure_ascii=False), user),
    )
    eid = cur.lastrowid
    conn.execute(
        'INSERT INTO example_validations '
        '(example_id, spec_version_id, status, errors, basis_version_id) '
        'VALUES (?,?,?,?,?)',
        (eid, spec_version_id,
         'valid' if not errors else 'invalid',
         json.dumps(errors, ensure_ascii=False), spec_version_id),
    )
    rebuild_index(conn, spec_version_id, eid, root, payload)
    conn.commit()
    return eid, errors


def examples_for_path(conn, spec_version_id, root, path):
    """按参数路径反查示例（点击参数 -> 相关示例）。path 为空则列全部。"""
    ver = conn.execute('SELECT * FROM spec_versions WHERE id=?',
                       (spec_version_id,)).fetchone()
    if not ver:
        raise KeyError('版本不存在')
    spec = json.loads(ver['raw'])

    if path:
        target = f'{root}:{path}'
        rows = conn.execute(
            'SELECT example_id, param_path FROM param_example_index '
            'WHERE spec_version_id=?', (spec_version_id,)).fetchall()
        eids = {r['example_id'] for r in rows
                if r['param_path'].startswith(f'{root}:')
                and paths_related(r['param_path'], target)}
    elif root:
        eids = {r['id'] for r in
                conn.execute('SELECT id FROM examples WHERE spec_name=? AND root=?',
                             (ver['spec_name'], root)).fetchall()}
    else:
        # 导出场景：root=None 表示整份快照的全部示例
        eids = {r['id'] for r in
                conn.execute('SELECT id FROM examples WHERE spec_name=?',
                             (ver['spec_name'],)).fetchall()}

    out = []
    for eid in sorted(eids):
        ex = conn.execute('SELECT * FROM examples WHERE id=?', (eid,)).fetchone()
        val = conn.execute(
            'SELECT * FROM example_validations WHERE example_id=? AND spec_version_id=?',
            (eid, spec_version_id)).fetchone()
        payload = json.loads(ex['payload'])
        out.append({
            'id': ex['id'],
            'title': ex['title'],
            'root': ex['root'],
            'payload': payload,
            'status': val['status'] if val else 'unvalidated',
            'errors': json.loads(val['errors']) if val else [],
            'basis_version_id': val['basis_version_id'] if val else None,
            'current_version_id': spec_version_id,
            'field_states': describe_payload(spec, ex['root'], payload),
        })
    return out
