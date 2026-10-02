"""规范版本落库、参数级 diff、受影响示例标记与复验。"""
from __future__ import annotations

import hashlib
import json

from .resolver import Resolver
from .tree import DEFAULT_MAX_DEPTH, build_full, iter_nodes
from .validate import validate_value
from .examples import paths_related, rebuild_index


def _fingerprint(node):
    """只含会影响示例合法性的约束；deprecated/描述等展示性字段不参与。"""
    material = {
        'type': node.get('type'),
        'required': node.get('required'),
        'conditions': [c['ast'] for c in node.get('conditions', [])],
        'context': [c['ast'] for c in node.get('context', [])],
        'nullable': node.get('nullable'),
        'default': node.get('default'),
        'enum': node.get('enum'),
        'const': node.get('const'),
        'minimum': node.get('minimum'),
        'maximum': node.get('maximum'),
        'minItems': node.get('minItems'),
        'maxItems': node.get('maxItems'),
        'minLength': node.get('minLength'),
        'maxLength': node.get('maxLength'),
        'pattern': node.get('pattern'),
    }
    return hashlib.sha1(json.dumps(
        material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _parent_key(key):
    root, _, path = key.partition(':')
    if path.endswith('[]'):
        parent = path[:-2]
    elif '.' in path:
        parent = path.rsplit('.', 1)[0]
    else:
        parent = ''
    return f'{root}:{parent}'


def param_map(spec, max_depth=DEFAULT_MAX_DEPTH):
    """{根:路径 -> {fingerprint, required}}，同名字段靠完整路径区分。"""
    resolver = Resolver(spec)
    out = {}
    for root in resolver.schema_names():
        tree = build_full(spec, root, max_depth)
        for node in iter_nodes(tree):
            if node['kind'] in ('field', 'items', 'reference') and node['path']:
                key = f'{root}:{node["path"]}'
                out[key] = {'fingerprint': _fingerprint(node),
                            'required': node['required']}
    return out


def diff_param_maps(old, new):
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old
                     and new[k]['fingerprint'] != old[k]['fingerprint'])
    # 「新增全局必填」：v2 无条件必填，而 v1 不是无条件必填
    # （即使 v2 仍保留其它条件规则，required 列表里有它就意味着所有分支必填）。
    newly_required = sorted(
        k for k in new
        if new[k]['required'] == 'required'
        and (k not in old or old[k]['required'] != 'required'))
    return {'added': added, 'removed': removed,
            'changed': changed, 'newly_required': newly_required}


def create_version(conn, spec_name, version, spec, user='anonymous'):
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(raw.encode()).hexdigest()

    prev = conn.execute(
        'SELECT * FROM spec_versions WHERE spec_name=? ORDER BY id DESC LIMIT 1',
        (spec_name,)).fetchone()

    new_map = param_map(spec)
    diff = {'added': [], 'removed': [], 'changed': [], 'newly_required': []}
    affected = set()
    if prev:
        old_map = param_map(json.loads(prev['raw']))
        diff = diff_param_maps(old_map, new_map)
        affected = _affected_examples(conn, prev['id'], spec_name, diff)

    cur = conn.execute(
        'INSERT INTO spec_versions (spec_name, version, raw, sha256, '
        'diff_from_prev, created_by) VALUES (?,?,?,?,?,?)',
        (spec_name, version, raw, sha,
         json.dumps(diff, ensure_ascii=False), user),
    )
    vid = cur.lastrowid

    # 旧示例的校验结论不能冒充已适配新版：
    # 受影响 -> needs_revalidation；未受影响 -> 沿用上版本结论（basis 仍指旧版本）
    examples = conn.execute('SELECT * FROM examples WHERE spec_name=?',
                            (spec_name,)).fetchall()
    for ex in examples:
        old_val = conn.execute(
            'SELECT * FROM example_validations WHERE example_id=? AND spec_version_id=?',
            (ex['id'], prev['id'] if prev else -1)).fetchone()
        if ex['id'] in affected or old_val is None:
            conn.execute(
                'INSERT INTO example_validations '
                '(example_id, spec_version_id, status, errors, basis_version_id) '
                'VALUES (?,?,?,?,?)',
                (ex['id'], vid, 'needs_revalidation', '[]',
                 prev['id'] if prev else vid),
            )
        else:
            conn.execute(
                'INSERT INTO example_validations '
                '(example_id, spec_version_id, status, errors, basis_version_id) '
                'VALUES (?,?,?,?,?)',
                (ex['id'], vid, old_val['status'], old_val['errors'],
                 old_val['basis_version_id']),
            )
        payload = json.loads(ex['payload'])
        rebuild_index(conn, vid, ex['id'], ex['root'], payload)

    conn.commit()
    return vid, diff, sorted(affected)


def _affected_examples(conn, prev_vid, spec_name, diff):
    """依据反向索引计算受参数变更影响的示例集合。"""
    rows = conn.execute(
        'SELECT pei.example_id, pei.param_path, e.root '
        'FROM param_example_index pei JOIN examples e ON e.id = pei.example_id '
        'WHERE pei.spec_version_id=?', (prev_vid,)).fetchall()
    ex_paths = {}
    for r in rows:
        ex_paths.setdefault(r['example_id'], set()).add(r['param_path'])

    affected = set()
    hot = diff['changed'] + diff['removed']
    for eid, paths in ex_paths.items():
        if any(paths_related(p, q) for p in paths for q in hot):
            affected.add(eid)

    all_examples = conn.execute(
        'SELECT id, root FROM examples WHERE spec_name=?',
        (spec_name,)).fetchall()
    for key in diff['newly_required']:
        root, _, path = key.partition(':')
        parent = _parent_key(key).partition(':')[2]
        if not parent:
            # 顶层新增必填：该 root 下所有示例都受影响
            affected.update(e['id'] for e in all_examples if e['root'] == root)
        else:
            for eid, paths in ex_paths.items():
                if any(paths_related(p, key) or paths_related(p, _parent_key(key))
                       for p in paths):
                    affected.add(eid)
    return affected


def revalidate(conn, spec_version_id):
    """对 needs_revalidation 的示例按当前版本快照重新校验。"""
    ver = conn.execute('SELECT * FROM spec_versions WHERE id=?',
                       (spec_version_id,)).fetchone()
    if not ver:
        raise KeyError('版本不存在')
    spec = json.loads(ver['raw'])
    resolver = Resolver(spec)
    rows = conn.execute(
        "SELECT ev.example_id, e.root, e.payload FROM example_validations ev "
        "JOIN examples e ON e.id = ev.example_id "
        "WHERE ev.spec_version_id=? AND ev.status='needs_revalidation'",
        (spec_version_id,)).fetchall()
    counts = {'revalidated': 0, 'valid': 0, 'invalid': 0}
    for row in rows:
        payload = json.loads(row['payload'])
        errors = validate_value(resolver.root_schema(row['root']), payload, resolver)
        status = 'valid' if not errors else 'invalid'
        conn.execute(
            "UPDATE example_validations SET status=?, errors=?, "
            "basis_version_id=?, validated_at=datetime('now') "
            "WHERE example_id=? AND spec_version_id=?",
            (status, json.dumps(errors, ensure_ascii=False),
             spec_version_id, row['example_id'], spec_version_id),
        )
        rebuild_index(conn, spec_version_id, row['example_id'],
                      row['root'], payload)
        counts['revalidated'] += 1
        counts[status] += 1
    conn.commit()
    return counts
