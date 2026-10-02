"""领域模型：节点、需求谓词、诊断、路径状态。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# 结构深度与递归展开的安全上限
MAX_STRUCT_DEPTH = 30
MAX_REF_EXPAND = 3          # 同一 $ref 定义在一条参数路径上最多物化层数
INDEX_NODE_BUDGET = 400     # 发布时索引走查的节点预算

# 节点类型
OBJECT = "object"
ARRAY = "array"
SCALAR = "scalar"
REF = "ref"
BRANCH = "branch"
ERROR = "error"

# 必填性
RQ_REQUIRED = "required"
RQ_OPTIONAL = "optional"
RQ_FORBIDDEN = "forbidden"

# 示例逐路径状态
ST_PRESENT = "present"
ST_NULL = "null"
ST_DEFAULTED = "defaulted"
ST_ABSENT = "absent"
ST_MISSING = "missing"
ST_INVALID = "invalid"

# 示例复验状态
EX_PASSED = "passed"
EX_FAILED = "failed"
EX_PENDING = "pending"
EX_STALE = "stale"

# 诊断码
REF_DANGLING = "ref-dangling"
REF_CYCLE = "ref-cycle"
BRANCH_CONFLICT = "branch-conflict"
SCHEMA_CONFLICT = "schema-conflict"
FORBIDDEN_NODE = "forbidden-node"


@dataclass
class Diagnostic:
    code: str
    message: str
    ref: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Requirement:
    """一个必填性谓词。when=None 表示无条件。"""
    require: str                      # required | optional | forbidden
    when: str | None = None           # 谓词表达式，如 "kind == 'person'"
    source: str = ""                  # 分支 id / schema 位置
    branch_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Node:
    """参数树节点（一次惰性展开的一层结果）。"""
    node_id: str
    name: str
    kind: str
    param_path: str                   # 参数路径，如 address.zip / items[].id
    loc: list[str]                    # JSON Pointer 风格的结构位置
    depth: int
    expanded: bool                    # 子层是否已物化（false=可再展开的桩）
    expandable: bool
    requiredness: list[Requirement] = field(default_factory=list)
    has_default: bool = False
    default: Any = None
    nullable: bool = False
    deprecated: bool = False
    enum: list[Any] | None = None
    value_type: str | None = None     # string/integer/.../object/array/mixed
    description: str | None = None
    ref_target: str | None = None     # ref 桩指向的定义
    recursive: bool = False           # 是否为合法自引用桩
    branch_label: str | None = None
    condition: str | None = None      # branch 伪节点/分支的条件
    diagnostics: list[Diagnostic] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    # 展示分组：required | conditional | optional | deprecated | branch
    def group(self) -> str:
        if self.kind == BRANCH:
            return "branch"
        if self.deprecated:
            return "deprecated"
        reqs = self.requiredness
        has_uncond_required = any(r.when is None and r.require == RQ_REQUIRED for r in reqs)
        has_uncond_forbidden = any(r.when is None and r.require == RQ_FORBIDDEN for r in reqs)
        if has_uncond_required and not has_uncond_forbidden:
            return "required"
        if any(r.when is not None for r in reqs) or (has_uncond_required and has_uncond_forbidden):
            return "conditional"
        return "optional"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["group"] = self.group()
        d["children"] = [c.to_dict() for c in self.children]
        return d
