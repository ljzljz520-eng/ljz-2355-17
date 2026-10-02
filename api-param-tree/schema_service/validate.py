"""示例校验：默认值 / 缺省 / 显式 null 严格三态。

- 字段缺省（key 不在对象里）：不违反类型，只看 required；若有 default 视为取默认值。
- 显式 null：仅在 nullable 或 type 含 null 时合法 —— 与「缺省」不是一回事。
- $ref 合法递归：用 (ref, id(data)) 去重，不会误报自引用；
  无法解析的引用记为错误而不是拖垮整棵校验。
"""
from __future__ import annotations

from .resolver import RefResolutionError, Resolver, merge_schema
from .tree import make_effective
from . import conditions as C


def validate_value(schema, data, resolver, path='$', _seen=None):
    errors = []
    _validate(schema, data, path, resolver, errors, _seen if _seen is not None else set())
    # 同一路径 + 错误码去重：if/then、dependentRequired、分支校验可能重复触达
    deduped, seen_err = [], set()
    for e in errors:
        k = (e['path'], e['code'])
        if k not in seen_err:
            seen_err.add(k)
            deduped.append(e)
    return deduped


def _type_match(value, t):
    if isinstance(t, list):
        return any(_type_match(value, x) for x in t)
    if t == 'string':
        return isinstance(value, str)
    if t == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if t == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == 'boolean':
        return isinstance(value, bool)
    if t == 'object':
        return isinstance(value, dict)
    if t == 'array':
        return isinstance(value, list)
    if t == 'null':
        return value is None
    return True


def _branch_match(branch_schema, data, path, resolver, errors, seen):
    """以「不产生错误」判定分支是否命中；返回 (是否命中, 错误列表)。"""
    branch_errors = []
    _validate(branch_schema, data, path, resolver, branch_errors, seen)
    return len(branch_errors) == 0, branch_errors


def _validate(schema, data, path, resolver, errors, seen):
    if not isinstance(schema, dict):
        return

    if '$ref' in schema:
        ref = schema['$ref']
        key = (ref, id(data))
        if key in seen:
            return  # 合法递归：同一引用对象已在校验栈上
        try:
            target = resolver.resolve_ref(ref)
        except RefResolutionError as exc:
            errors.append({'path': path, 'code': 'unresolved', 'message': str(exc)})
            return
        sib = {k: v for k, v in schema.items() if k != '$ref'}
        merged = merge_schema(target, sib) if sib else target
        seen.add(key)
        _validate(merged, data, path, resolver, errors, seen)
        seen.discard(key)
        return

    for part in schema.get('allOf', []):
        _validate(part, data, path, resolver, errors, seen)

    # ---- 显式 null：缺省根本不会走到这里 ----
    if data is None:
        t = schema.get('type')
        nullable = bool(schema.get('nullable')) or (isinstance(t, list) and 'null' in t) or t == 'null'
        constrained = any(k in schema for k in ('type', 'properties', 'required', 'items'))
        if constrained and not nullable:
            errors.append({'path': path, 'code': 'null_not_allowed',
                           'message': '不允许显式 null（注意：缺省、null 与默认值是三态）'})
        if 'enum' in schema and None not in schema['enum']:
            errors.append({'path': path, 'code': 'enum', 'message': 'null 不在枚举内'})
        if 'const' in schema and schema['const'] is not None:
            errors.append({'path': path, 'code': 'const', 'message': 'null 不等于常量'})
        return

    t = schema.get('type')
    if t is not None and not _type_match(data, t):
        errors.append({'path': path, 'code': 'type',
                       'message': '类型应为 ' + str(t) + '，实际为 '
                                  + type(data).__name__})
    if 'enum' in schema and data not in schema['enum']:
        errors.append({'path': path, 'code': 'enum', 'message': '值不在枚举内'})
    if 'const' in schema and data != schema['const']:
        errors.append({'path': path, 'code': 'const', 'message': '值不等于常量'})

    # 数值 / 长度 / 模式约束
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if 'minimum' in schema and data < schema['minimum']:
            errors.append({'path': path, 'code': 'minimum',
                           'message': '不能小于 ' + str(schema['minimum'])})
        if 'maximum' in schema and data > schema['maximum']:
            errors.append({'path': path, 'code': 'maximum',
                           'message': '不能大于 ' + str(schema['maximum'])})
    if isinstance(data, str):
        if 'minLength' in schema and len(data) < schema['minLength']:
            errors.append({'path': path, 'code': 'minLength',
                           'message': '长度不能小于 ' + str(schema['minLength'])})
        if 'maxLength' in schema and len(data) > schema['maxLength']:
            errors.append({'path': path, 'code': 'maxLength',
                           'message': '长度不能大于 ' + str(schema['maxLength'])})
        if 'pattern' in schema:
            import re
            if not re.search(schema['pattern'], data):
                errors.append({'path': path, 'code': 'pattern',
                               'message': '不匹配模式 ' + schema['pattern']})
    if isinstance(data, list):
        if 'minItems' in schema and len(data) < schema['minItems']:
            errors.append({'path': path, 'code': 'minItems',
                           'message': '元素数量不能少于 ' + str(schema['minItems'])})
        if 'maxItems' in schema and len(data) > schema['maxItems']:
            errors.append({'path': path, 'code': 'maxItems',
                           'message': '元素数量不能多于 ' + str(schema['maxItems'])})

    req = schema.get('required', [])
    props = schema.get('properties', {})
    if isinstance(data, dict):
        for key in req:
            if key not in data:
                sub = props.get(key)
                # 三态：字段缺省但声明了 default —— 按默认值生效，不算缺失。
                # 注意这与「显式 null」是两回事，后者仍走 nullable 校验。
                if isinstance(sub, dict) and 'default' in sub:
                    continue
                errors.append({'path': path + '.' + key, 'code': 'missing_required',
                               'message': '缺少必填字段 ' + key})
        if schema.get('additionalProperties') is False:
            for key in data:
                if key not in props:
                    errors.append({'path': path + '.' + key,
                                   'code': 'additional_not_allowed',
                                   'message': '不允许未声明的字段 ' + key})
        for name, value in data.items():
            if name in props:
                _validate(props[name], value, path + '.' + name,
                          resolver, errors, seen)
        for key, deps in (schema.get('dependentRequired') or {}).items():
            if key in data:
                for dep in deps:
                    if dep not in data:
                        errors.append({
                            'path': path + '.' + dep, 'code': 'dependent_required',
                            'message': '字段 ' + key + ' 存在时 ' + dep + ' 必填'})

    if 'items' in schema and isinstance(data, list):
        for i, item in enumerate(data):
            _validate(schema['items'], item, f'{path}[{i}]',
                      resolver, errors, seen)

    # ---- if/then/else：条件求值，不把条件必填当全局必填 ----
    if 'if' in schema:
        if_errors = []
        _validate(schema['if'], data, path, resolver, if_errors, seen)
        if len(if_errors) == 0:
            if 'then' in schema:
                _validate(schema['then'], data, path, resolver, errors, seen)
        elif 'else' in schema:
            _validate(schema['else'], data, path, resolver, errors, seen)

    # ---- anyOf / oneOf：分支命中数 ----
    for kind in ('anyOf', 'oneOf'):
        branches = schema.get(kind)
        if not branches:
            continue
        matched = 0
        first_errors = None
        for b in branches:
            ok, branch_errors = _branch_match(b, data, path, resolver, errors, seen)
            if ok:
                matched += 1
            elif first_errors is None:
                first_errors = branch_errors
        if kind == 'anyOf' and matched == 0:
            errors.append({'path': path, 'code': 'anyof_mismatch',
                           'message': '不匹配 anyOf 任何分支'})
        if kind == 'oneOf' and matched != 1:
            errors.append({'path': path, 'code': 'oneof_mismatch',
                           'message': 'oneOf 命中 ' + str(matched)
                                      + ' 个分支（要求恰好 1 个）'})


def describe_payload(spec, root, data):
    """根对象各字段的三态：value / null / absent / absent_default。"""
    resolver = Resolver(spec)
    try:
        status, payload = make_effective(resolver.root_schema(root), resolver, [])
    except KeyError:
        return []
    if status != 'ok':
        return []
    eff, _, _ = payload
    required = eff.get('required') or []
    rows = []
    for name, sub in (eff.get('properties') or {}).items():
        if isinstance(data, dict) and name in data:
            state = 'null' if data[name] is None else 'value'
        else:
            has_default = isinstance(sub, dict) and 'default' in sub
            state = 'absent_default' if has_default else 'absent'
        rows.append({
            'name': name,
            'state': state,
            'required': name in required,
            'default': sub.get('default') if isinstance(sub, dict) else None,
        })
    return rows
