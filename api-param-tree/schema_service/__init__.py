"""API 文档参数树服务。

模块划分：
- resolver   : $ref 解析与 schema 合并
- conditions : 条件必填的条件 AST（构建 / 求值 / 人类可读渲染）
- tree       : 参数树构建（全量展开与按节点惰性展开共用同一内核）
- validate   : 示例校验（区分 缺省 / null / 值 三态）
- versions   : 规范版本落库、参数级 diff、复验标记
- examples   : 示例管理与「参数 -> 示例」反向索引
- shares     : 可分享展开状态令牌与访问权限校验
- server     : 标准库 HTTP API + 页面
"""

__version__ = "1.0.0"
