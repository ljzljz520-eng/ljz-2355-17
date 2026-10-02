"""Schema 解析器：$ref 与组合关键字、条件必填、递归/错环、惰性句柄。"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import model as M


def jget(doc: Any, loc: list[str] | str) -> Any:
    """按 JSON Pointer 段取文档；找不到返回 _MISSING。"""
    if isinstance(loc, str):
        pointer = loc
        if pointer in ("", "#", "#/"):
            return doc
        if not pointer.startswith("#"):
            return _MISSING
        parts = pointer[2:].split("/") if pointer.startswith("#/") else []
    else:
        parts = loc
    cur = doc
    for p in parts:
        token = p.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                cur = cur[int(token)]
            except (ValueError, IndexError):
                return _MISSING
        elif isinstance(cur, dict) and token in cur:
            cur = cur[token]
        else:
            return _MISSING
    return cur


_MISSING = object()


def ref_loc(ref: str) -> list[str]:
    return ref[2:].split("/") if ref.startswith("#/") else []


# ---------- 句柄编解码 ----------

def encode_handle(h: dict[str, Any]) -> str:
    raw = json.dumps(h, separators=(",", ":"), sort_keys=True).encode()
    return "n_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_handle(node_id: str) -> dict[str, Any]:
    raw = node_id[2:] if node_id.startswith("n_") else node_id
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw.encode()))


# ---------- 中间结构 ----------

@dataclass
class Header:
    value_type: str | None = None
    has_default: bool = False
    default: Any = None
    nullable: bool = False
    deprecated: bool = False
    enum: list[Any] | None = None
    description: str | None = None


@dataclass
class PropContrib:
    loc: list[str]
    header: Header
    reqs: list[M.Requirement] = field(default_factory=list)
    diagnostics: list[M.Diagnostic] = field(default_factory=list)


@dataclass
class BranchDef:
    bid: str
    kind: str               # oneof | anyof | ifthen | ifelse
    cond: str | None
    label: str
    bloc: list[str]
    idx: int
    props: dict[str, PropContrib] = field(default_factory=dict)


@dataclass
class Merge:
    header: Header
    props: dict[str, PropContrib] = field(default_factory=dict)
    branches: list[BranchDef] = field(default_factory=list)
    forbidden: set[str] = field(default_factory=set)
    diagnostics: list[M.Diagnostic] = field(default_factory=list)
    schema_kind: str = M.OBJECT


def is_nullable(schema: dict[str, Any]) -> bool:
    t = schema.get("type")
    if isinstance(t, list) and "null" in t:
        return True
    if isinstance(schema.get("enum"), list) and None in schema["enum"]:
        return True
    return False


def type_name(schema: dict[str, Any]) -> str | None:
    t = schema.get("type")
    if isinstance(t, list):
        ts = [x for x in t if x != "null"]
        return "/".join(ts) if ts else "null"
    return t


def header_of(schema: dict[str, Any]) -> Header:
    return Header(
        value_type=type_name(schema), has_default="default" in schema,
        default=schema.get("default"), nullable=is_nullable(schema),
        deprecated=bool(schema.get("deprecated", False)),
        enum=schema.get("enum") if isinstance(schema.get("enum"), list) else None,
        description=schema.get("description"))


def merge_header(dst: Header, src: Header) -> Header:
    if dst.value_type is None and src.value_type:
        dst.value_type = src.value_type
    if not dst.has_default and src.has_default:
        dst.has_default, dst.default = True, src.default
    dst.nullable |= src.nullable
    dst.deprecated |= src.deprecated
    if dst.enum is None and src.enum is not None:
        dst.enum = src.enum
    if dst.description is None and src.description:
        dst.description = src.description
    return dst


def compile_predicate(schema: dict[str, Any]) -> str | None:
    """if 子 schema → 可读谓词（与 validator._pred_match 同构）。"""
    conds: list[str] = []
    if "const" in schema:
        conds.append(f"$ == {json.dumps(schema['const'], ensure_ascii=False)}")
    if isinstance(schema.get("enum"), list) and len(schema["enum"]) == 1:
        conds.append(f"$ == {json.dumps(schema['enum'][0], ensure_ascii=False)}")
    if isinstance(schema.get("type"), str):
        conds.append(f"type($) == {json.dumps(schema['type'])}")
    for p, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            if "const" in sub:
                conds.append(f"{p} == {json.dumps(sub['const'], ensure_ascii=False)}")
            elif isinstance(sub.get("enum"), list) and len(sub["enum"]) == 1:
                conds.append(f"{p} == {json.dumps(sub['enum'][0], ensure_ascii=False)}")
            if isinstance(sub.get("type"), str):
                conds.append(f"type({p}) == {json.dumps(sub['type'])}")
    for p in schema.get("required", []) or []:
        conds.append(f"defined({p})")
    return " && ".join(conds) if conds else None


def variant_condition(variant: dict[str, Any]) -> str | None:
    conds: list[str] = []
    for p, sub in (variant.get("properties") or {}).items():
        if isinstance(sub, dict):
            if "const" in sub:
                conds.append(f"{p} == {json.dumps(sub['const'], ensure_ascii=False)}")
            elif isinstance(sub.get("enum"), list) and len(sub["enum"]) == 1:
                conds.append(f"{p} == {json.dumps(sub['enum'][0], ensure_ascii=False)}")
    return " && ".join(conds) if conds else None


class Resolver:
    """把一份不可变 schema 文档解析为惰性参数树。

    句柄中携带 loc（文档结构位置）、chain/occ（$ref 激活栈与出现计数）、
    bctx（分支上下文），使每一次惰性展开都可以无状态重放。
    """

    def __init__(self, snapshot_id: str, doc: dict[str, Any],
                 can_read: Callable[[str], bool] | None = None):
        self.snap = snapshot_id
        self.doc = doc
        self.can_read = can_read or (lambda p: True)

    # ------------------------------------------------------------------
    # $ref 跳转：递归 vs 错误环的唯一判定处
    # ------------------------------------------------------------------
    def _follow(self, schema: Any, occ: dict[str, int],
                chain: list[tuple[str, int]], depth: int):
        """沿裸 $ref 链跳转（非生产性，深度不增长）。

        返回：
          ("ok",        (target, occ, chain, final_loc))
          ("recursion", (ref, target, occ, chain))   # 合法自引用（跨了结构边）
          ("refcap",    ref)                          # 合法递归但展开预算用尽
          ("cycle",     (ref, chain))                 # 非生产性环（错误引用环）
          ("dangling",  ref)
        递归/环判定时 occ/chain 已把当前这一跳计入（物化语义）。
        """
        cur = schema
        loc_occ = dict(occ)
        loc_chain = list(chain)
        final_ref = None
        while isinstance(cur, dict) and "$ref" in cur:
            ref = cur["$ref"]
            target = jget(self.doc, ref)
            if target is _MISSING:
                return "dangling", ref
            for active_ref, entry_depth in loc_chain:
                if active_ref == ref:
                    if depth > entry_depth:
                        # 距上次激活跨过了属性/items 等生产性边 → 合法自引用
                        if loc_occ.get(ref, 0) >= M.MAX_REF_EXPAND:
                            return "refcap", ref
                        loc_occ[ref] = loc_occ.get(ref, 0) + 1
                        loc_chain.append((ref, depth))
                        return "recursion", (ref, target, dict(loc_occ), list(loc_chain))
                    # 纯别名 / 纯 allOf 闭合，无生产性边 → 错误引用环
                    return "cycle", (ref, list(loc_chain))
            # 首次进入该定义：仅压栈（occ 的出现次数由“回边”递增）
            loc_chain.append((ref, depth))
            final_ref = ref
            cur = target
        return "ok", (cur, loc_occ, loc_chain, ref_loc(final_ref) if final_ref else None)

    # ------------------------------------------------------------------
    # 组合展开
    # ------------------------------------------------------------------
    def _submerge(self, schema: Any, occ: dict[str, int],
                  chain: list[tuple[str, int]], depth: int,
                  site_loc: list[str]) -> tuple[Merge | None, dict, list, str | None]:
        """先去引用再合取；返回的 base loc 指向最终目标（保证子句柄可重放）。"""
        st, payload = self._follow(schema, occ, chain, depth)
        if st == "dangling":
            return None, occ, chain, payload
        if st == "cycle":
            ref, cyc = payload
            return Merge(
                header=Header(), schema_kind=M.ERROR,
                diagnostics=[M.Diagnostic(
                    M.REF_CYCLE,
                    f"检测到非生产性引用环（错误引用环）: "
                    f"{' -> '.join(r for r, _ in cyc)} -> {ref}", ref=ref)]
            ), occ, chain, ref
        if st in ("recursion", "refcap"):
            # 组合位置出现自引用且未跨结构层：按错误环处理（allOf 别名闭合）
            ref = payload[0] if st == "recursion" else payload
            return Merge(
                header=Header(), schema_kind=M.ERROR,
                diagnostics=[M.Diagnostic(
                    M.REF_CYCLE,
                    f"组合位置出现未跨结构层的自引用 {ref}，按错误环处理", ref=ref)]
            ), occ, chain, ref
        target, nocc, nchain, final_loc = payload
        base = final_loc if final_loc is not None else site_loc
        return self._merge(target, nocc, nchain, depth, base), nocc, nchain, None

    def _merge(self, schema: Any, occ: dict[str, int],
               chain: list[tuple[str, int]], depth: int, base_loc: list[str]) -> Merge:
        if schema is False:
            return Merge(header=Header(), schema_kind=M.ERROR,
                         diagnostics=[M.Diagnostic(M.SCHEMA_CONFLICT,
                                                   "schema=false：无合法值")])
        if not isinstance(schema, dict):
            return Merge(header=Header(), schema_kind=M.SCALAR)

        diag: list[M.Diagnostic] = []
        merged_header = Header()
        props: dict[str, PropContrib] = {}
        branches: list[BranchDef] = []
        forbidden: set[str] = set()

        # 1) 自身片段（去掉组合键）
        own = {k: v for k, v in schema.items()
               if k not in ("allOf", "oneOf", "anyOf", "if", "then", "else", "not")}
        if isinstance(own, dict):
            own_h = header_of(own) if own else Header()
            t = own.get("type")
            if t == "object" or "properties" in own or (
                    isinstance(t, list) and "object" in t):
                merge_header(merged_header, own_h)
                required = list(own.get("required", []))
                for pname, psub in (own.get("properties") or {}).items():
                    ploc = base_loc + ["properties", pname]
                    pc = props.setdefault(pname, PropContrib(loc=ploc, header=Header()))
                    st, payload = self._follow(psub, occ, chain, depth + 1)
                    if st == "ok":
                        target_schema = payload[0]
                        if isinstance(target_schema, dict):
                            merge_header(pc.header, header_of(target_schema))
                    pc.reqs.append(M.Requirement(
                        M.RQ_REQUIRED if pname in required else M.RQ_OPTIONAL,
                        source="/".join(ploc)))
            elif t is not None or own.get("enum") is not None or "default" in own:
                merge_header(merged_header, own_h)

        # 2) allOf：合取（非生产性边）
        for i, part in enumerate(schema.get("allOf", [])):
            sm, nocc, nchain, err = self._submerge(
                part, occ, chain, depth, base_loc + ["allOf", str(i)])
            if sm is None:
                diag.append(M.Diagnostic(M.REF_DANGLING,
                                         f"allOf[{i}] 引用缺失: {err}", ref=err))
                continue
            diag.extend(sm.diagnostics)
            merge_header(merged_header, sm.header)
            for pname, pc in sm.props.items():
                dst = props.setdefault(pname, PropContrib(loc=pc.loc, header=Header()))
                merge_header(dst.header, pc.header)
                dst.reqs.extend(pc.reqs)
            forbidden |= sm.forbidden

        # 3) oneOf / anyOf：变体分支（不打平成一个对象）
        base_required = set(schema.get("required", []))
        for ckey, ckind in (("oneOf", "oneof"), ("anyOf", "anyof")):
            variants = schema.get(ckey)
            if not isinstance(variants, list):
                continue
            for i, variant in enumerate(variants):
                sm, _, _, err = self._submerge(
                    variant, occ, chain, depth, base_loc + [ckey, str(i)])
                if sm is None:
                    diag.append(M.Diagnostic(M.REF_DANGLING,
                                             f"{ckey}[{i}] 引用缺失: {err}", ref=err))
                    continue
                diag.extend(sm.diagnostics)
                variant_schema = jget(self.doc, sm and self._site_of(
                    variant, base_loc + [ckey, str(i)])) if False else None
                # 条件取自原始变体（含 $ref 目标）
                vs = variant
                vst, vpayload = self._follow(variant, occ, chain, depth)
                if vst == "ok":
                    vs = vpayload[0]
                cond = variant_condition(vs) if isinstance(vs, dict) else None
                bid = f"b{len(branches)}"
                bd = BranchDef(bid=bid, kind=ckind, cond=cond,
                               label=cond or f"variant {i + 1}",
                               bloc=base_loc, idx=i, props=dict(sm.props))
                branches.append(bd)
                vrequired = set(vs.get("required", [])) if isinstance(vs, dict) else set()
                for pname in (vrequired - base_required):
                    pc = props.setdefault(pname, PropContrib(
                        loc=base_loc + [ckey, str(i), "properties", pname],
                        header=Header()))
                    # 用变体里的真实属性描述补全 header / loc
                    vp = sm.props.get(pname)
                    if vp is not None:
                        merge_header(pc.header, vp.header)
                        pc.loc = list(vp.loc)
                    pc.reqs = [r for r in pc.reqs if r.require != M.RQ_OPTIONAL]
                    pc.reqs.append(M.Requirement(
                        M.RQ_REQUIRED, when=cond or f"variant {i + 1}",
                        source=bid, branch_label=bd.label))

        # 4) if / then / else
        if isinstance(schema.get("if"), dict):
            cond = compile_predicate(schema["if"]) or "if-schema"
            for key, tag in (("then", "ifthen"), ("else", "ifelse")):
                part = schema.get(key)
                if not isinstance(part, dict):
                    continue
                sm, _, _, _ = self._submerge(part, occ, chain, depth,
                                             base_loc + [key])
                if sm is None:
                    continue
                diag.extend(sm.diagnostics)
                bid = f"b{len(branches)}"
                c = cond if key == "then" else f"!({cond})"
                branches.append(BranchDef(
                    bid=bid, kind=tag, cond=c,
                    label=("IF " if key == "then" else "ELSE ") + c,
                    bloc=base_loc, idx=0 if key == "then" else 1,
                    props=dict(sm.props)))
                for pname, pc in sm.props.items():
                    dst = props.setdefault(pname, PropContrib(
                        loc=pc.loc, header=Header()))
                    merge_header(dst.header, pc.header)
                for pname in (part.get("required", [])):
                    pc = props.setdefault(pname, PropContrib(
                        loc=base_loc + [key, "properties", pname], header=Header()))
                    vp = sm.props.get(pname)
                    if vp is not None:
                        merge_header(pc.header, vp.header)
                        pc.loc = list(vp.loc)
                    pc.reqs = [r for r in pc.reqs
                               if not (r.require == M.RQ_OPTIONAL and r.when is None)]
                    pc.reqs.append(M.Requirement(
                        M.RQ_REQUIRED, when=c, source=bid,
                        branch_label=("IF " if key == "then" else "ELSE ") + cond))
                # 无条件 forbidden 也要以同一分支条件挂接，冲突才能在同条件上显现
                for fpname in sm.forbidden:
                    fpc = props.setdefault(fpname, PropContrib(
                        loc=(sm.props.get(fpname).loc if fpname in sm.props
                             else base_loc + [key, "not", "properties", fpname]),
                        header=Header()))
                    fpc.reqs = [r for r in fpc.reqs
                                if not (r.require == M.RQ_FORBIDDEN and r.when is None)]
                    fpc.reqs.insert(0, M.Requirement(
                        M.RQ_FORBIDDEN, when=c, source=bid,
                        branch_label=("IF " if key == "then" else "ELSE ") + cond))
                forbidden |= sm.forbidden

        # 5) not：支持 not.required / not.properties 的禁止语义
        n = schema.get("not")
        if isinstance(n, dict):
            for pname in list(n.get("required", [])) + list((n.get("properties") or {}).keys()):
                forbidden.add(pname)
                pc = props.setdefault(pname, PropContrib(
                    loc=base_loc + ["not", "properties", pname], header=Header()))
                pc.reqs.insert(0, M.Requirement(M.RQ_FORBIDDEN, source="/".join(base_loc)))

        # 最终类型判定
        t = schema.get("type")
        if isinstance(t, list):
            ts = sorted({x for x in t if x != "null"})
            t = ts[0] if len(ts) == 1 else ("mixed" if ts else None)
        if t == "array" or "items" in schema:
            schema_kind = M.ARRAY
        elif t == "object" or props or branches or "properties" in schema:
            schema_kind = M.OBJECT
        elif t is not None:
            schema_kind = M.SCALAR
        else:
            schema_kind = M.OBJECT if props or branches else M.SCALAR

        self._detect_conflicts(props, diag, base_loc)
        return Merge(header=merged_header, props=props, branches=branches,
                     forbidden=forbidden, diagnostics=diag, schema_kind=schema_kind)

    @staticmethod
    def _site_of(schema: Any, site: list[str]) -> list[str]:
        return site

    @staticmethod
    def _detect_conflicts(props: dict[str, PropContrib],
                          diag: list[M.Diagnostic], loc: list[str]) -> None:
        for pname, pc in props.items():
            req_whens = {r.when for r in pc.reqs if r.require == M.RQ_REQUIRED}
            forb_whens = {r.when for r in pc.reqs if r.require == M.RQ_FORBIDDEN}
            overlap = req_whens & forb_whens
            if overlap:
                d = M.Diagnostic(
                    M.BRANCH_CONFLICT,
                    f"字段 {pname} 在同一条件下既必填又禁止: {sorted(str(w) for w in overlap)}",
                    path=pname)
                diag.append(d)
                pc.reqs.append  # no-op marker; attach diagnostic directly below
                pc.diagnostics.append(d)

    # ------------------------------------------------------------------
    # 句柄 → 节点
    # ------------------------------------------------------------------
    def root_handle(self) -> dict[str, Any]:
        return {"snap": self.snap, "loc": [], "name": "$", "depth": 0,
                "chain": [], "occ": {}, "bctx": [], "vi": None, "path": "$"}

    def render(self, handle: dict[str, Any], materialize_children: bool) -> M.Node:
        if handle.get("snap") != self.snap:
            raise SnapshotMismatch(handle.get("snap"), self.snap)
        if handle.get("kind") == M.BRANCH:
            return self._render_branch(handle, materialize_children)

        depth = handle.get("depth", 0)
        name, path = handle["name"], handle["path"]
        chain = [tuple(x) for x in handle.get("chain", [])]
        occ = handle.get("occ", {})

        schema = jget(self.doc, handle["loc"]) if handle["loc"] else self.doc
        st, payload = self._follow(schema, occ, chain, depth)

        if st == "dangling":
            return self._error_node(handle, M.REF_DANGLING,
                                    f"引用缺失: {payload}", ref=payload)
        if st == "cycle":
            ref, cyc = payload
            return self._error_node(
                handle, M.REF_CYCLE,
                f"检测到非生产性引用环（错误引用环）: "
                f"{' -> '.join(r for r, _ in cyc)} -> {ref}", ref=ref)
        if st in ("recursion", "refcap"):
            if st == "recursion":
                ref, target, nocc, nchain = payload
                expandable = nocc.get(ref, 0) < M.MAX_REF_EXPAND and depth < M.MAX_STRUCT_DEPTH
                merge = self._merge(target, nocc, nchain, depth, ref_loc(ref))
            else:
                ref = payload
                target = jget(self.doc, ref)
                nocc, nchain = occ, chain
                expandable = False
                merge = (self._merge(target, nocc, nchain, depth, ref_loc(ref))
                         if target is not _MISSING
                         else Merge(header=Header(), schema_kind=M.ERROR))
            node = self._node_from_merge(
                handle, merge, materialize_children, nocc, nchain, ref_loc(ref))
            # 始终是「引用节点」：折叠时是桩；展开时给出一层物化子节点，
            # 更深的回边会在子层中再次以引用桩出现（occ 已递增，受深度上限约束）。
            node.kind = M.REF
            node.ref_target = ref
            node.recursive = True
            node.expandable = expandable
            node.expanded = materialize_children and bool(node.children)
            return node

        target, nocc, nchain, final_loc = payload
        if depth >= M.MAX_STRUCT_DEPTH:
            return M.Node(node_id=encode_handle(handle), name=name, kind=M.REF,
                          param_path=path, loc=handle["loc"], depth=depth,
                          expanded=False, expandable=False, recursive=True,
                          diagnostics=[M.Diagnostic(M.REF_CYCLE, "达到结构深度上限")])
        base_loc = final_loc if final_loc is not None else handle["loc"]
        merge = self._merge(target, nocc, nchain, depth, base_loc)
        return self._node_from_merge(handle, merge, materialize_children,
                                     nocc, nchain, base_loc)

    def _error_node(self, handle: dict[str, Any], code: str, msg: str,
                    ref: str | None = None) -> M.Node:
        return M.Node(node_id=encode_handle(handle), name=handle["name"], kind=M.ERROR,
                      param_path=handle["path"], loc=handle["loc"],
                      depth=handle["depth"], expanded=False, expandable=False,
                      ref_target=ref,
                      diagnostics=[M.Diagnostic(code, msg, ref=ref)])

    def _node_from_merge(self, handle: dict[str, Any], merge: Merge,
                         materialize: bool, occ: dict[str, int],
                         chain: list[tuple[str, int]], base_loc: list[str]) -> M.Node:
        h = merge.header
        frozen = handle.get("h")
        if frozen:
            value_type = frozen.get("vt")
            has_default = frozen.get("hd", False)
            default = frozen.get("df")
            nullable = frozen.get("nu", False)
            deprecated = frozen.get("dp", False)
            enum = frozen.get("en")
            description = frozen.get("de")
        else:
            value_type = h.value_type
            has_default, default = h.has_default, h.default
            nullable, deprecated, enum, description = (
                h.nullable, h.deprecated, h.enum, h.description)

        kind = {M.OBJECT: M.OBJECT, M.ARRAY: M.ARRAY,
                M.SCALAR: M.SCALAR, M.ERROR: M.ERROR}.get(merge.schema_kind, M.OBJECT)
        node = M.Node(
            node_id=encode_handle(handle), name=handle["name"], kind=kind,
            param_path=handle["path"], loc=handle["loc"], depth=handle["depth"],
            expanded=False, expandable=True, has_default=has_default, default=default,
            nullable=nullable, deprecated=deprecated, enum=enum, value_type=value_type,
            description=description, diagnostics=list(merge.diagnostics))
        if handle.get("rq"):
            node.requiredness = [M.Requirement(**r) for r in handle["rq"]]

        if not materialize:
            node.children = []
            node.expandable = (merge.schema_kind in (M.OBJECT, M.ARRAY)
                               or bool(merge.branches))
            return node

        children: list[M.Node] = []

        if merge.schema_kind == M.ARRAY:
            items_handle = {"snap": self.snap, "loc": base_loc + ["items"],
                            "name": "[]（数组项）", "depth": handle["depth"] + 1,
                            "chain": [list(c) for c in chain], "occ": dict(occ),
                            "bctx": handle.get("bctx", []), "vi": None,
                            "path": handle["path"] + "[]"}
            children.append(self.render(items_handle, False))

        if merge.schema_kind in (M.OBJECT, M.ERROR) or merge.props or merge.branches:
            for pname, pc in merge.props.items():
                child_path = (pname if handle["path"] == "$"
                              else handle["path"] + "." + pname)
                if not self.can_read(child_path):
                    children.append(self._forbidden_node(
                        handle, pname, child_path, pc.loc, occ, chain))
                    continue
                ch = {"vt": pc.header.value_type, "hd": pc.header.has_default,
                      "df": pc.header.default, "nu": pc.header.nullable,
                      "dp": pc.header.deprecated, "en": pc.header.enum,
                      "de": pc.header.description}
                child_handle = {"snap": self.snap, "loc": pc.loc, "name": pname,
                                "depth": handle["depth"] + 1,
                                "chain": [list(c) for c in chain], "occ": dict(occ),
                                "bctx": handle.get("bctx", []), "vi": None,
                                "path": child_path, "h": ch,
                                "rq": [r.to_dict() for r in pc.reqs]}
                child_node = self.render(child_handle, False)
                child_node.requiredness = list(pc.reqs)
                child_node.diagnostics.extend(pc.diagnostics)
                children.append(child_node)

            for bd in merge.branches:
                br_handle = {"snap": self.snap, "loc": base_loc,
                             "name": "branch:" + bd.bid, "kind": M.BRANCH,
                             "depth": handle["depth"] + 1,
                             "chain": [list(c) for c in chain], "occ": dict(occ),
                             "bctx": handle.get("bctx", []) + [
                                 {"id": bd.bid, "cond": bd.cond, "label": bd.label}],
                             "vi": bd.bid, "path": handle["path"],
                             "bloc": bd.bloc, "bidx": bd.idx, "bkind": bd.kind,
                             "bcond": bd.cond, "blabel": bd.label}
                children.append(self.render(br_handle, False))

        node.children = children
        node.expanded = bool(children)
        node.expandable = (merge.schema_kind in (M.OBJECT, M.ARRAY)
                           or bool(merge.branches))
        return node

    def _render_branch(self, handle: dict[str, Any], materialize: bool) -> M.Node:
        bd_kind, bloc, idx = handle["bkind"], handle["bloc"], handle["bidx"]
        container = jget(self.doc, bloc) if bloc else self.doc
        if bd_kind == "oneof":
            variant = container.get("oneOf", [])[idx] if isinstance(container, dict) else {}
        elif bd_kind == "anyof":
            variant = container.get("anyOf", [])[idx] if isinstance(container, dict) else {}
        elif bd_kind == "ifthen":
            variant = container.get("then", {}) if isinstance(container, dict) else {}
        else:
            variant = container.get("else", {}) if isinstance(container, dict) else {}

        depth = handle["depth"]
        chain = [tuple(x) for x in handle.get("chain", [])]
        occ = handle.get("occ", {})
        st, payload = self._follow(variant, occ, chain, depth)
        node = M.Node(node_id=encode_handle(handle),
                      name=handle.get("blabel", "branch"), kind=M.BRANCH,
                      param_path=handle["path"] + "::" + handle["vi"],
                      loc=bloc, depth=depth, expanded=False, expandable=True,
                      condition=handle.get("bcond"),
                      branch_label=handle.get("blabel"))
        if st == "dangling":
            node.diagnostics.append(M.Diagnostic(M.REF_DANGLING, "分支引用缺失"))
            return node
        if st in ("cycle", "refcap"):
            node.diagnostics.append(M.Diagnostic(M.REF_CYCLE, "分支引用环"))
            return node
        target, nocc, nchain, final_loc = payload
        merge = self._merge(target, nocc, nchain, depth,
                            final_loc if final_loc is not None else
                            bloc + [bd_kind.replace("onef", "oneOf")
                                    .replace("anyf", "anyOf"), str(idx)])
        node.diagnostics.extend(merge.diagnostics)
        if not materialize:
            return node
        children: list[M.Node] = []
        for pname, pc in merge.props.items():
            child_path = (pname if handle["path"] in ("$", "")
                          else handle["path"] + "." + pname)
            if not self.can_read(child_path):
                children.append(self._forbidden_node(
                    handle, pname, child_path, pc.loc, nocc, nchain))
                continue
            ch = {"vt": pc.header.value_type, "hd": pc.header.has_default,
                  "df": pc.header.default, "nu": pc.header.nullable,
                  "dp": pc.header.deprecated, "en": pc.header.enum,
                  "de": pc.header.description}
            child_handle = {"snap": self.snap, "loc": pc.loc, "name": pname,
                            "depth": depth + 1, "chain": [list(c) for c in nchain],
                            "occ": dict(nocc), "bctx": handle.get("bctx", []),
                            "vi": handle["vi"], "path": child_path, "h": ch,
                            "rq": [r.to_dict() for r in pc.reqs]}
            cn = self.render(child_handle, False)
            cn.requiredness = list(pc.reqs)
            children.append(cn)
        node.children = children
        node.expanded = bool(children)
        return node

    def _forbidden_node(self, parent_handle: dict[str, Any], name: str, path: str,
                        loc: list[str], occ: dict[str, int],
                        chain: list[tuple[str, int]]) -> M.Node:
        h = {"snap": self.snap, "loc": loc, "name": name,
             "depth": parent_handle["depth"] + 1,
             "chain": [list(c) for c in chain], "occ": dict(occ),
             "bctx": parent_handle.get("bctx", []), "vi": None, "path": path}
        return M.Node(node_id=encode_handle(h), name=name, kind=M.ERROR,
                      param_path=path, loc=loc, depth=h["depth"],
                      expanded=False, expandable=False,
                      diagnostics=[M.Diagnostic(M.FORBIDDEN_NODE, "无访问权限")])

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def root_tree(self) -> M.Node:
        return self.render(self.root_handle(), True)

    def expand(self, node_id: str) -> M.Node:
        h = decode_handle(node_id)
        if h.get("snap") != self.snap:
            raise SnapshotMismatch(h.get("snap"), self.snap)
        return self.render(h, True)

    def shallow(self, node_id: str) -> M.Node:
        h = decode_handle(node_id)
        if h.get("snap") != self.snap:
            raise SnapshotMismatch(h.get("snap"), self.snap)
        return self.render(h, False)

    def iter_index(self) -> Iterator[tuple[str, M.Node]]:
        """发布时的安全上限走查：产出参数目录（树渲染仍保持惰性）。"""
        budget = M.INDEX_NODE_BUDGET
        root = self.root_tree()
        stack: list[M.Node] = [root]
        seen: set[str] = set()
        while stack and budget > 0:
            node = stack.pop()
            budget -= 1
            if node.param_path in seen:
                continue
            seen.add(node.param_path)
            yield node.param_path, node
            if node.children:
                stack.extend(reversed(node.children))
            elif node.expandable and node.kind != M.ERROR:
                try:
                    materialized = self.expand(node.node_id)
                except SnapshotMismatch:
                    continue
                stack.extend(reversed(materialized.children))


class SnapshotMismatch(Exception):
    def __init__(self, got: str, expected: str):
        self.got, self.expected = got, expected
        super().__init__(f"snapshot-mismatch: {got} != {expected}")
