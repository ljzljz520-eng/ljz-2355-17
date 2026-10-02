"""示例校验：与 Resolver 同构的 $ref/递归语义，输出逐路径状态。"""
from __future__ import annotations

from typing import Any

from . import model as M
from .resolver import Resolver, is_nullable, jget


def _pred_match(schema: dict[str, Any], value: Any) -> bool:
    """if 子 schema 求值（compile_predicate 的运行时对应物）。"""
    if not isinstance(value, dict):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if isinstance(schema.get("enum"), list) and len(schema["enum"]) == 1:
        if value != schema["enum"][0]:
            return False
    if isinstance(schema.get("type"), str) and not _type_is(value, schema["type"]):
        return False
    for p, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        if "const" in sub and value.get(p) != sub["const"]:
            return False
        if isinstance(sub.get("enum"), list) and len(sub["enum"]) == 1:
            if value.get(p) != sub["enum"][0]:
                return False
        if isinstance(sub.get("type"), str) and p in value and not _type_is(value[p], sub["type"]):
            return False
    for p in schema.get("required", []) or []:
        if p not in value:
            return False
    return True


def _type_is(value: Any, type_name: str) -> bool:
    return {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "null": lambda v: v is None,
    }.get(type_name, lambda v: True)(value)


class ExampleValidator:
    def __init__(self, resolver: Resolver):
        self.r = resolver
        self.doc = resolver.doc
        self.paths: dict[str, str] = {}
        self.issues: list[dict[str, Any]] = []

    def validate(self, payload: Any) -> dict[str, Any]:
        self.paths, self.issues = {}, []
        self._check(self.doc, payload, "$", [], {}, [], 0)
        return {"state": M.EX_PASSED if not self.issues else M.EX_FAILED,
                "issues": self.issues, "paths": self.paths}

    def _follow(self, schema: Any, occ: dict[str, int],
                chain: list[tuple[str, int]], depth: int):
        return self.r._follow(schema, occ, chain, depth)

    def _check(self, schema: Any, value: Any, path: str,
               loc: list[str], occ: dict[str, int],
               chain: list[tuple[str, int]], depth: int) -> None:
        if depth > M.MAX_STRUCT_DEPTH:
            return
        st, payload = self._follow(schema, occ, chain, depth)
        if st == "dangling":
            self.issues.append({"path": path, "code": M.REF_DANGLING,
                                "message": f"引用缺失 {payload}"})
            return
        if st == "cycle":
            ref, _ = payload
            self.issues.append({"path": path, "code": M.REF_CYCLE,
                                "message": f"错误引用环 {ref}"})
            return
        if st == "refcap":
            self.paths.setdefault(path, M.ST_PRESENT)
            return
        if st == "recursion":
            _, eff, nocc, nchain = payload
        else:
            eff, nocc, nchain, _ = payload

        if eff is False:
            self.issues.append({"path": path, "code": "not-allowed",
                                "message": "schema=false，不允许出现值"})
            return
        if not isinstance(eff, dict):
            self.paths[path] = M.ST_PRESENT
            return

        # allOf 聚合
        parts = [{k: v for k, v in eff.items()
                  if k not in ("allOf", "oneOf", "anyOf", "if", "then", "else", "not")}]
        parts.extend(eff.get("allOf", []))

        types: list[str] = []
        required_sets: list[set[str]] = []
        forbidden: set[str] = set()
        props_map: dict[str, Any] = {}
        enum_vals = None
        nullable = False

        for part in parts:
            pst, pp = self._follow(part, nocc, nchain, depth)
            if pst not in ("ok", "recursion"):
                continue
            p = pp[0] if pst == "recursion" else pp[0]
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(t)
            nullable |= is_nullable(p)
            if isinstance(p.get("enum"), list):
                enum_vals = p["enum"]
            required_sets.append(set(p.get("required", [])))
            for pn, ps in (p.get("properties") or {}).items():
                props_map.setdefault(pn, ps)
            n = p.get("not")
            if isinstance(n, dict):
                forbidden |= set(n.get("required", []))
                forbidden |= set((n.get("properties") or {}).keys())

        # if/then/else
        if isinstance(eff.get("if"), dict):
            branch = eff.get("then", {}) if _pred_match(eff["if"], value) else eff.get("else", {})
            pst, pp = self._follow(branch, nocc, nchain, depth)
            if pst in ("ok", "recursion"):
                pe = pp[0]
                if isinstance(pe, dict):
                    required_sets.append(set(pe.get("required", [])))
                    for pn, ps in (pe.get("properties") or {}).items():
                        props_map.setdefault(pn, ps)
                    n = pe.get("not")
                    if isinstance(n, dict):
                        for fp in list(n.get("required", [])) + list((n.get("properties") or {}).keys()):
                            forbidden.add(fp)

        # oneOf/anyOf
        variants = eff.get("oneOf") or eff.get("anyOf")
        if isinstance(variants, list):
            matched = None
            for v in variants:
                pst, pp = self._follow(v, nocc, nchain, depth)
                if pst in ("ok", "recursion") and isinstance(pp[0], dict) \
                        and self._variant_match(pp[0], value, nocc, nchain, depth):
                    matched = pp[0]
                    break
            if matched is None and "oneOf" in eff:
                self.issues.append({"path": path, "code": "no-branch",
                                    "message": "oneOf 没有匹配的分支"})
            if matched is not None:
                required_sets.append(set(matched.get("required", [])))
                for pn, ps in (matched.get("properties") or {}).items():
                    props_map.setdefault(pn, ps)

        # 值：null / 缺省三分
        if value is None:
            self.paths[path] = M.ST_NULL
            if "null" not in types and not nullable:
                self.issues.append({"path": path, "code": "type",
                                    "message": "null 用于不允许 null 的字段"})
            return
        non_null_types = [t for t in types if t != "null"]
        if non_null_types:
            if not any(_type_is(value, t) for t in set(non_null_types)):
                self.issues.append({"path": path, "code": "type",
                                    "message": f"类型应为 {sorted(set(non_null_types))}"})
                self.paths[path] = M.ST_INVALID
                return
        if enum_vals is not None and value not in enum_vals:
            self.issues.append({"path": path, "code": "enum",
                                "message": f"值 {value!r} 不在 enum 中"})
            self.paths[path] = M.ST_INVALID
            return
        self.paths[path] = M.ST_PRESENT

        if isinstance(value, dict):
            required = set().union(*required_sets) if required_sets else set()
            for pn in forbidden:
                cp = pn if path == "$" else f"{path}.{pn}"
                if pn in value:
                    self.issues.append({"path": cp, "code": "forbidden",
                                        "message": "该字段在当前分支被禁止"})
                    self.paths[cp] = M.ST_INVALID
            for pn in required:
                cp = pn if path == "$" else f"{path}.{pn}"
                if pn not in value:
                    sub = props_map.get(pn, {})
                    pst, pp = self._follow(sub, nocc, nchain, depth + 1)
                    if pst in ("ok", "recursion") and isinstance(pp[0], dict) \
                            and "default" in pp[0]:
                        self.paths[cp] = M.ST_DEFAULTED
                    else:
                        self.paths[cp] = M.ST_MISSING
                        self.issues.append({"path": cp, "code": "required",
                                            "message": f"缺少必填字段 {pn}"})
            for pn, sub in props_map.items():
                cp = pn if path == "$" else f"{path}.{pn}"
                if pn in value:
                    if value[pn] is None:
                        self.paths[cp] = M.ST_NULL
                        pst, pp = self._follow(sub, nocc, nchain, depth + 1)
                        if pst in ("ok", "recursion") and isinstance(pp[0], dict) \
                                and not self._allows_null(pp[0], nocc, nchain, depth):
                            self.issues.append({"path": cp, "code": "type",
                                                "message": "null 用于不允许 null 的字段"})
                        continue
                    self._check(sub, value[pn], cp,
                                loc + ["properties", pn], dict(nocc), list(nchain),
                                depth + 1)
                else:
                    pst, pp = self._follow(sub, nocc, nchain, depth + 1)
                    if pst in ("ok", "recursion") and isinstance(pp[0], dict) \
                            and "default" in pp[0]:
                        self.paths[cp] = M.ST_DEFAULTED
                    elif pn not in required:
                        self.paths[cp] = M.ST_ABSENT

        elif isinstance(value, list):
            items_schema = eff.get("items", {})
            for i, item in enumerate(value):
                self._check(items_schema, item, f"{path}[{i}]",
                            loc + ["items"], dict(nocc), list(nchain), depth + 1)

    def _allows_null(self, schema: dict[str, Any], occ, chain, depth) -> bool:
        if is_nullable(schema):
            return True
        for s in schema.get("allOf", []):
            pst, pp = self._follow(s, occ, chain, depth)
            if pst in ("ok", "recursion") and isinstance(pp[0], dict) \
                    and self._allows_null(pp[0], occ, chain, depth):
                return True
        return False

    def _variant_match(self, variant: dict[str, Any], value: Any,
                       occ=None, chain=None, depth=0) -> bool:
        if not isinstance(value, dict):
            return False
        occ, chain = occ or {}, chain or []
        # 判别字段以变体自身 schema 为准（不能被基础属性的 enum/type 遮蔽）
        for p, sub in (variant.get("properties") or {}).items():
            pst, pp = self._follow(sub, occ, chain, depth)
            sub_eff = pp[0] if pst in ("ok", "recursion") else sub
            if isinstance(sub_eff, dict):
                if "const" in sub_eff and value.get(p) != sub_eff["const"]:
                    return False
                if isinstance(sub_eff.get("enum"), list) and len(sub_eff["enum"]) == 1:
                    if value.get(p) != sub_eff["enum"][0]:
                        return False
        # 注意：变体的 required 是“选定该分支后”的条件必填，
        # 不能反过来作为选择分支的判别条件（否则缺 tax_id 时无法判定为 company 分支）。
        if isinstance(variant.get("type"), str) and not _type_is(value, variant["type"]):
            return False
        return True
