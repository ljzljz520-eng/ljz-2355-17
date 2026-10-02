"""$ref 解析与 schema 合并（allOf 的语义合并在这里做）。"""
from __future__ import annotations


class RefResolutionError(Exception):
    def __init__(self, ref):
        self.ref = ref
        super().__init__('无法解析的引用: ' + ref)


class Resolver:
    """针对单个规范快照的引用解析器（快照不可变，解析结果稳定）。"""

    def __init__(self, spec: dict):
        self.spec = spec

    def resolve_ref(self, ref: str):
        if not isinstance(ref, str) or not ref.startswith('#/'):
            raise RefResolutionError(ref)
        node = self.spec
        for raw in ref[2:].split('/'):
            part = raw.replace('~1', '/').replace('~0', '~')
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise RefResolutionError(ref)
        return node

    @staticmethod
    def ref_name(ref: str) -> str:
        return ref.rsplit('/', 1)[-1]

    def schema_names(self):
        return list(self.spec.get('components', {}).get('schemas', {}).keys())

    def root_schema(self, name: str):
        schemas = self.spec.get('components', {}).get('schemas', {})
        if name not in schemas:
            raise KeyError('根 schema 不存在: ' + name)
        return schemas[name]


def merge_schema(base, over):
    """深合并两个 schema 片段：properties 逐键递归合并，required 取并集，
    其余键后者覆盖前者。用于 $ref 兄弟键与 allOf 合并。"""
    if not (isinstance(base, dict) and isinstance(over, dict)):
        return over
    out = dict(base)
    for key, value in over.items():
        if key == 'properties':
            if isinstance(out.get('properties'), dict) and isinstance(value, dict):
                props = dict(out['properties'])
                for name, sub in value.items():
                    if name in props:
                        props[name] = merge_schema(props[name], sub)
                    else:
                        props[name] = sub
                out['properties'] = props
            else:
                out[key] = value
        elif key == 'required':
            if isinstance(out.get('required'), list) and isinstance(value, list):
                out['required'] = list(dict.fromkeys(out['required'] + value))
            else:
                out[key] = value
        elif key == 'allOf':
            if isinstance(out.get('allOf'), list) and isinstance(value, list):
                out['allOf'] = out['allOf'] + value
            else:
                out[key] = value
        else:
            out[key] = value
    return out
