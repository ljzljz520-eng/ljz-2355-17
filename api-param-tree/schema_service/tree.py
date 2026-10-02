"""参数树构建：全量展开与按节点惰性展开共用同一内核。

关键设计：
1. 条件必填不压成布尔值 —— 节点 required ∈ {required, conditional, optional}，
   条件以 AST 形式挂在 conditions / context 上。
2. 合法递归显示为「引用节点」并限制展开深度；纯别名环（无结构出口）才是错误，
   且只标记出错子树，不拖垮整棵树。
3. 节点 id 是从根出发的边序列（p~名 / i / o~kind~idx），可稳定重放，
   因此惰性展开、全量展开、分享状态恢复走同一条代码路径，结果必然一致。
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote, unquote

from . import conditions as C
from .resolver import Resolver, RefResolutionError, merge_schema

# 能终结引用环的结构边：经过属性或数组元素的自引用是「合法递归」
STRUCTURAL_EDGES = ('property', 'items')
DEFAULT_MAX_DEPTH = 3


class NodeNotFound(Exception):
    pass


class DepthExceeded(Exception):
    pass


def make_effective(schema, resolver, trail):
    """沿 $ref 链解析并合并 allOf，返回 (status, data)。

    status = "ok"        -> data = (effective_schema, trail, cond_sources)
    status = "reference" -> data = {"ref", "chain"}   合法递归：显示为引用节点
    status = "error"     -> data = {"code", "message"} 错误引用环 / 未解析引用
    """
    cur = schema
    siblings = {}
    local_trail = list(trail)
    hops = 0
    while isinstance(cur, dict) and '$ref' in cur:
        ref = cur['$ref']
        hit = _find_ref(local_trail, ref)
        if hit >= 0:
            segment = local_trail[hit + 1:]
            productive = any(ev[0] == 'edge' and ev[1] in STRUCTURAL_EDGES
                             for ev in segment)
            # 经 allOf 再次见到同一引用：发生在属性/元素内部，属于合法自引用，
            # 保留为引用节点（展开受深度限制），而不是把结构合并到无限大。
            ref_productively = any(ev[0] == 'edge' and ev[1] == 'allOf'
                                   for ev in segment)
            chain = [ev[1] for ev in local_trail[hit:] if ev[0] == 'ref'] + [ref]
            if productive or ref_productively:
                return 'reference', {'ref': ref, 'chain': chain}
            else:
                return 'error', {
                    'code': 'cycle',
                    'message': '引用环 %s 没有结构出口（未经过属性/数组元素），无法展开'
                               % ' → '.join(Resolver.ref_name(r) for r in chain),
                }
        try:
            target = resolver.resolve_ref(ref)
        except RefResolutionError as exc:
            return 'error', {'code': 'unresolved', 'message': str(exc)}
        sib = {k: v for k, v in cur.items() if k != '$ref'}
        siblings = merge_schema(siblings, sib) if siblings else sib
        local_trail = local_trail + [('ref', ref)]
        cur = target
        hops += 1
        if hops > 100:
            return 'error', {'code': 'cycle', 'message': '引用链过长，疑似环'}

    if not isinstance(cur, dict):
        return 'error', {'code': 'invalid', 'message': 'schema 不是对象'}

    eff = dict(cur)
    if siblings:
        eff = merge_schema(eff, siblings)

    cond_sources = []
    if any(k in eff for k in ('if', 'then', 'else', 'dependentRequired')):
        cond_sources.append({k: eff[k] for k in
                             ('if', 'then', 'else', 'dependentRequired') if k in eff})

    if 'allOf' in eff:
        parts = eff.pop('allOf')
        for part in parts:
            status, data = make_effective(part, resolver,
                                          local_trail + [('edge', 'allOf')])
            if status != 'ok':
                return status, data
            sub_eff, _, sub_conds = data
            cond_sources.extend(sub_conds)
            eff = merge_schema(eff, sub_eff)

    return 'ok', (eff, local_trail, cond_sources)


def _find_ref(trail, ref):
    for i, ev in enumerate(trail):
        if ev[0] == 'ref' and ev[1] == ref:
            return i
    return -1


def conditions_by_field(cond_sources):
    """把 if/then/else 与 dependentRequired 转成 {字段名: [条件 AST]}。"""
    out = {}
    for src in cond_sources:
        ast = C.from_if(src['if']) if 'if' in src else None
        if ast is not None:
            for name in (src.get('then') or {}).get('required', []):
                out.setdefault(name, []).append(ast)
            for name in (src.get('else') or {}).get('required', []):
                out.setdefault(name, []).append(C.not_(ast))
        for key, deps in (src.get('dependentRequired') or {}).items():
            for dep in (deps or []):
                out.setdefault(dep, []).append(C.present(key))
    return out


def _seg(name):
    return 'p~' + quote(name, safe='')


def _join(parent_id, seg):
    return parent_id + '/' + seg if parent_id else seg


def _resolve_node(spec, root, node_id, max_depth):
    """把 node_id（边序列）重放为节点上下文。引用节点被穿越时消耗一层深度。"""
    resolver = Resolver(spec)
    root_ref = '#/components/schemas/' + root
    try:
        raw = resolver.root_schema(root)
    except KeyError as exc:
        raise NodeNotFound(str(exc)) from exc

    # 根节点本身就是 root_ref 的定义；登记锚点后，allOf 内对根的自引用
    # （经过属性/元素到达）才能被识别为「合法递归」而不是被无限合并。
    trail = [('ref', root_ref)]
    inherited = []
    path = ''
    remaining = max_depth
    name = root
    kind_hint = 'root'
    branch_info = None
    parent_ctx = {'required': [], 'conds': {}, 'inherited': []}

    segments = node_id.split('/') if node_id else []
    for seg in segments:
        # r~ 是「展开引用节点」的合成边：消耗一层深度并把 trail 锚到该 ref。
        # 不编码这一层，重放时深度预算会被重置，合法自引用在全量构建中永不终止。
        if seg == 'r~':
            if remaining <= 0:
                raise DepthExceeded(node_id)
            remaining -= 1
            status, data = make_effective(raw, resolver, trail)
            if status != 'reference':
                raise NodeNotFound('该位置不是可展开的引用')
            raw = resolver.resolve_ref(data['ref'])
            trail = [('ref', data['ref'])]
            continue
        status, data = make_effective(raw, resolver, trail)
        if status == 'reference':
            if remaining <= 0:
                raise DepthExceeded(node_id)
            remaining -= 1
            raw = resolver.resolve_ref(data['ref'])
            trail = [('ref', data['ref'])]
            continue
        if status != 'ok':
            raise NodeNotFound(data.get('message', '节点不可用'))
        eff, trail, cond_sources = data
        cbf = conditions_by_field(cond_sources)

        if seg.startswith('p~'):
            nm = unquote(seg[2:])
            props = eff.get('properties') or {}
            if nm not in props:
                raise NodeNotFound('字段不存在: ' + nm)
            parent_ctx = {
                'required': eff.get('required') or [],
                'conds': cbf,
                'inherited': list(inherited),
            }
            raw = props[nm]
            name = nm
            kind_hint = 'field'
            trail = trail + [('edge', 'property')]
            path = f'{path}.{nm}' if path else nm
        elif seg == 'i':
            if 'items' not in eff:
                raise NodeNotFound('该节点不是数组')
            parent_ctx = {'required': [], 'conds': {}, 'inherited': list(inherited)}
            raw = eff['items']
            name = '(元素)'
            kind_hint = 'items'
            trail = trail + [('edge', 'items')]
            path = path + '[]'
        elif seg.startswith('o~'):
            _, kind, idx = seg.split('~')
            branches = eff.get(kind) or []
            idx = int(idx)
            if idx >= len(branches):
                raise NodeNotFound('分支不存在: ' + seg)
            inherited = inherited + [C.branch(kind, idx)]
            parent_ctx = {'required': [], 'conds': {}, 'inherited': list(inherited)}
            raw = branches[idx]
            name = f'{kind} 分支 {idx + 1}'
            kind_hint = 'branch'
            branch_info = (kind, idx)
            trail = trail + [('edge', kind)]
        else:
            raise NodeNotFound('非法节点段: ' + seg)

    return SimpleNamespace(
        resolver=resolver, raw=raw, trail=trail, inherited=inherited,
        path=path, remaining=remaining, name=name, kind_hint=kind_hint,
        branch_info=branch_info, parent_ctx=parent_ctx,
    )


def _type_of(eff):
    t = eff.get('type')
    if t:
        return t
    if 'properties' in eff:
        return 'object'
    if 'items' in eff:
        return 'array'
    return 'any'


def _build_node(ctx, node_id):
    """根据节点上下文生成节点元数据；同时返回有效 schema 信息（若有）。"""
    node = {
        'id': node_id,
        'name': ctx.name,
        'path': ctx.path,
        'kind': 'field' if ctx.kind_hint in ('root', 'field') else ctx.kind_hint,
        'type': 'any',
        'required': 'optional',
        'conditions': [],
        'context': [],
        'deprecated': False,
        'nullable': False,
        'default': {'present': False},
        'hasChildren': False,
    }
    if ctx.branch_info:
        node['branch'] = {'kind': ctx.branch_info[0], 'index': ctx.branch_info[1]}

    own_conds = (ctx.parent_ctx['conds'].get(ctx.name, [])
                 if ctx.kind_hint == 'field' else [])
    base_required = (ctx.kind_hint == 'field'
                     and ctx.name in ctx.parent_ctx['required'])
    inherited = ctx.parent_ctx['inherited']

    if base_required:
        # 出现在 required 列表中 = 所有分支必填（无条件必填）；
        # 即使同时挂着条件规则（如 dependentRequired），全局必填也是主导。
        node['required'] = 'required'
    elif own_conds:
        node['required'] = 'conditional'
    else:
        node['required'] = 'optional'

    node['conditions'] = [{'ast': a, 'text': '当 ' + C.humanize(a)} for a in own_conds]
    node['context'] = [{'ast': a, 'text': C.humanize(a)} for a in inherited]

    status, data = make_effective(ctx.raw, ctx.resolver, ctx.trail)
    if status == 'error':
        node.update(kind='error', message=data['message'], code=data['code'])
        return node, None
    if status == 'reference':
        node.update(
            kind='reference',
            ref=data['ref'],
            refName=Resolver.ref_name(data['ref']),
            chain=[Resolver.ref_name(r) for r in data['chain']],
            depthRemaining=ctx.remaining,
            hasChildren=ctx.remaining > 0,
        )
        return node, None

    eff, eff_trail, cond_sources = data
    node['type'] = _type_of(eff)
    node['deprecated'] = bool(eff.get('deprecated'))
    t = eff.get('type')
    node['nullable'] = bool(eff.get('nullable')) or (isinstance(t, list) and 'null' in t)
    if 'default' in eff:
        node['default'] = {'present': True, 'value': eff['default']}
    if 'enum' in eff:
        node['enum'] = eff['enum']
    if 'const' in eff:
        node['const'] = eff['const']
    for constraint in ('minimum', 'maximum', 'minItems', 'maxItems',
                       'minLength', 'maxLength', 'pattern'):
        if constraint in eff:
            node[constraint] = eff[constraint]
    if eff.get('description'):
        node['description'] = eff['description']
    node['hasChildren'] = bool(
        eff.get('properties')
        or 'items' in eff
        or eff.get('oneOf')
        or eff.get('anyOf')
    )
    return node, (eff, eff_trail, cond_sources)


def _children_of_effective(parent_id, parent_path, eff, eff_trail, cond_sources,
                           resolver, inherited, remaining, id_prefix=''):
    # id_prefix 用于引用展开：孩子 id 先经过 r~ 合成边（记录深度消耗），
    # 再走正常属性/元素/分支边，保证重放与现场展开结果一致。
    join_base = parent_id + ('/' + id_prefix if id_prefix else '')
    children = []
    cbf = conditions_by_field(cond_sources)
    required = eff.get('required') or []
    parent_ctx = {'required': required, 'conds': cbf, 'inherited': list(inherited)}

    for name, sub in (eff.get('properties') or {}).items():
        cid = _join(join_base, _seg(name))
        cpath = f'{parent_path}.{name}' if parent_path else name
        cctx = SimpleNamespace(
            resolver=resolver, raw=sub,
            trail=eff_trail + [('edge', 'property')],
            inherited=inherited, path=cpath, remaining=remaining,
            name=name, kind_hint='field', branch_info=None,
            parent_ctx=parent_ctx,
        )
        children.append(_build_node(cctx, cid)[0])

    if 'items' in eff:
        cid = _join(join_base, 'i')
        cctx = SimpleNamespace(
            resolver=resolver, raw=eff['items'],
            trail=eff_trail + [('edge', 'items')],
            inherited=inherited, path=parent_path + '[]', remaining=remaining,
            name='(元素)', kind_hint='items', branch_info=None,
            parent_ctx={'required': [], 'conds': {}, 'inherited': list(inherited)},
        )
        children.append(_build_node(cctx, cid)[0])

    for kind in ('oneOf', 'anyOf'):
        for idx, sub in enumerate(eff.get(kind) or []):
            cid = _join(join_base, f'o~{kind}~{idx}')
            branch_inherited = inherited + [C.branch(kind, idx)]
            cctx = SimpleNamespace(
                resolver=resolver, raw=sub,
                trail=eff_trail + [('edge', kind)],
                inherited=branch_inherited, path=parent_path, remaining=remaining,
                name=f'{kind} 分支 {idx + 1}', kind_hint='branch',
                branch_info=(kind, idx),
                parent_ctx={'required': [], 'conds': {},
                            'inherited': list(branch_inherited)},
            )
            children.append(_build_node(cctx, cid)[0])

    return children


def _depth_limit_node(parent_id):
    return {
        'id': _join(parent_id, 'depth-limit'),
        'name': '已达展开深度上限',
        'path': '',
        'kind': 'depth-limit',
        'type': 'any',
        'required': 'optional',
        'conditions': [],
        'context': [],
        'deprecated': False,
        'nullable': False,
        'default': {'present': False},
        'hasChildren': False,
    }


def compute_children(spec, root, node_id, max_depth):
    """惰性展开内核：给定节点 id，返回 (节点元数据, 子节点列表)。"""
    ctx = _resolve_node(spec, root, node_id, max_depth)
    node, eff_info = _build_node(ctx, node_id)

    if node['kind'] in ('error', 'depth-limit'):
        return node, []

    if node['kind'] == 'reference':
        if ctx.remaining <= 0:
            return node, [_depth_limit_node(node_id)]
        target = ctx.resolver.resolve_ref(node['ref'])
        ref_trail = [('ref', node['ref'])]
        status, data = make_effective(target, ctx.resolver, ref_trail)
        if status != 'ok':
            return node, []
        eff, eff_trail, cond_sources = data
        children = _children_of_effective(
            node_id, ctx.path, eff, eff_trail, cond_sources,
            ctx.resolver, ctx.inherited, ctx.remaining - 1, id_prefix='r~')
        return node, children

    eff, eff_trail, cond_sources = eff_info
    children = _children_of_effective(
        node_id, ctx.path, eff, eff_trail, cond_sources,
        ctx.resolver, ctx.inherited, ctx.remaining)
    return node, children


def build_full(spec, root, max_depth=DEFAULT_MAX_DEPTH):
    """构建期全量展开：与惰性展开同一内核，递归到无子节点为止。"""
    node, children = compute_children(spec, root, '', max_depth)

    def attach(n):
        if n['hasChildren'] and n['kind'] not in ('error', 'depth-limit'):
            _, ch = compute_children(spec, root, n['id'], max_depth)
            n['children'] = [attach(c) for c in ch]
        else:
            n['children'] = []
        return n

    node['children'] = [attach(c) for c in children]
    return node


def iter_nodes(node):
    """先序遍历全量树的节点。"""
    yield node
    for child in node.get('children') or []:
        yield from iter_nodes(child)
