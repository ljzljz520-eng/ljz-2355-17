"""条件必填的条件 AST：构建（from_if）/ 求值（evaluate）/ 渲染（humanize）。

必填性可能取决于分支（if/then/else、dependentRequired、oneOf/anyOf），
因此不能压成全局布尔值 —— 这里统一表示成可序列化、可求值的条件树。
"""
from __future__ import annotations

import json

# 条件 AST 是纯 dict，便于落库与分享：
#   {'op':'eq','path','value'} / {'op':'present','path'} / {'op':'type','path','type'}
#   {'op':'and'|'or','conds':[...]} / {'op':'not','cond':...}
#   {'op':'branch','kind','index'}  oneOf/anyOf 第 index 个分支被选中。

class _Absent:
    __slots__ = ()

    def __repr__(self):
        return '<absent>'


_ABSENT = _Absent()


def eq(path, value):
    return {'op': 'eq', 'path': path, 'value': value}


def present(path):
    return {'op': 'present', 'path': path}


def type_is(path, type_name):
    return {'op': 'type', 'path': path, 'type': type_name}


def and_(*conds):
    flat = []
    for c in conds:
        if c.get('op') == 'and':
            flat.extend(c['conds'])
        else:
            flat.append(c)
    return {'op': 'and', 'conds': flat}


def or_(*conds):
    return {'op': 'or', 'conds': list(conds)}


def not_(cond):
    return {'op': 'not', 'cond': cond}


def branch(kind, index):
    return {'op': 'branch', 'kind': kind, 'index': index}


def from_if(if_schema):
    """从 if 子 schema 提取条件 AST；无法表达时返回 None。"""
    if not isinstance(if_schema, dict):
        return None
    parts = []
    for name, sub in if_schema.get('properties', {}).items():
        if not isinstance(sub, dict):
            continue
        if 'const' in sub:
            parts.append(eq(name, sub['const']))
        elif 'enum' in sub and isinstance(sub['enum'], list):
            if len(sub['enum']) == 1:
                parts.append(eq(name, sub['enum'][0]))
            else:
                parts.append(or_(*[eq(name, v) for v in sub['enum']]))
        if 'type' in sub:
            parts.append(type_is(name, sub['type']))
    for name in if_schema.get('required', []):
        parts.append(present(name))
    if not parts:
        return None
    if len(parts) > 1:
        return and_(*parts)
    return parts[0]


def _lookup(data, path):
    cur = data
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _ABSENT
        cur = cur[part]
    return cur


def _type_match(value, type_name):
    if isinstance(type_name, list):
        return any(_type_match(value, t) for t in type_name)
    checks = {
        'string': lambda v: isinstance(v, str),
        'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
        'number': lambda v: (isinstance(v, (int, float)) and not isinstance(v, bool)),
        'boolean': lambda v: isinstance(v, bool),
        'object': lambda v: isinstance(v, dict),
        'array': lambda v: isinstance(v, list),
        'null': lambda v: v is None,
    }
    if type_name == 'any':
        return True
    fn = checks.get(type_name)
    return fn(value) if fn else False


def evaluate(cond, data, branch_matcher=None):
    """对载荷求值。branch 条件需要 branch_matcher(kind, index) -> bool。"""
    op = cond['op']
    if op == 'eq':
        v = _lookup(data, cond['path'])
        return v is not _ABSENT and v == cond['value']
    if op == 'present':
        return _lookup(data, cond['path']) is not _ABSENT
    if op == 'type':
        v = _lookup(data, cond['path'])
        return v is not _ABSENT and _type_match(v, cond['type'])
    if op == 'and':
        return all(evaluate(c, data, branch_matcher) for c in cond['conds'])
    if op == 'or':
        return any(evaluate(c, data, branch_matcher) for c in cond['conds'])
    if op == 'not':
        return not evaluate(cond['cond'], data, branch_matcher)
    if op == 'branch':
        return bool(branch_matcher(cond['kind'], cond['index'])) if branch_matcher else False
    return False


def humanize(cond):
    op = cond['op']
    if op == 'eq':
        return cond['path'] + ' = ' + json.dumps(cond['value'], ensure_ascii=False)
    if op == 'present':
        return cond['path'] + ' 存在'
    if op == 'type':
        return cond['path'] + ' 为 ' + str(cond['type']) + ' 类型'
    if op == 'and':
        return ' 且 '.join(humanize(c) for c in cond['conds'])
    if op == 'or':
        return '(' + ' 或 '.join(humanize(c) for c in cond['conds']) + ')'
    if op == 'not':
        return '非(' + humanize(cond['cond']) + ')'
    if op == 'branch':
        return '匹配 ' + cond['kind'] + ' 第 ' + str(cond['index'] + 1) + ' 分支'
    return str(cond)
