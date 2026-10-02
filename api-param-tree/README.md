# API 文档参数树服务

为多版本 API 规范生成**参数树**：网页按「必填 / 条件必填 / 可选 / 废弃」展开，
schema 服务解析 `$ref` 与 `allOf/oneOf/anyOf/if-then` 组合条件，SQL 存放
不可变规范版本与「参数 → 示例」反向索引。页面表格、样例高亮、导出始终取
**同一个版本快照**。

## 快速开始

```bash
cd api-param-tree
pip install 无三方依赖（仅 Python 3.10+ 标准库）
./run.sh 8000            # 建库 + 种子（v1→示例→v2 复验）+ 启动
# 或：python3 seed.py --reset && python3 -m schema_service.server 8000
```

打开 http://127.0.0.1:8000 。请求头 `X-User-Id` 标识用户（页面可 `?u=alice`）。

种子用户：`alice`（order 的 admin）、`bob`（reader）、`eve`（reader）。

## 设计要点（对应需求逐条）

### 1. 必填性可能取决于分支 —— 不压成全局布尔值
节点 `required ∈ {"required","conditional","optional"}`：
- `required`：出现在 `required` 列表（所有分支必填）；
- `conditional`：仅在条件下必填，条件以 **AST** 挂在 `conditions`
  （来自 `if/then/else`、`dependentRequired`）或 `context`
  （来自 `oneOf/anyOf` 分支继承，含人类可读文本）。

`conditions.py` 提供 `from_if`（构建）、`evaluate`（按载荷求值）、
`humanize`（渲染成「当 payType = "card"」）。

### 2. 默认值 / 缺省 / null 三态分开
- 字段不在对象中 = **缺省**；声明了 `default` 时按默认值生效（校验不算缺失，
  示例三态标注为 `absent_default`），未声明则 `absent`；
- 显式 `null` 是另一个值：只有 `nullable:true` 或 `type` 含 `null` 才合法
  （`null_not_allowed`），节点有独立的 `nullable` 与 `default:{present,value}`。

### 3. 合法递归 vs 错误引用环（不能一律报错）
- **合法自引用**：两次命中同一 `$ref` 之间经过了结构边
  （`property` / `items`，或经 `allOf` 出现在属性内部）→ 显示为
  **引用节点**（`kind:"reference"`，带 refName/chain），**限制展开深度**
  （默认 3），到底显示 `depth-limit`。例：`Address.parent → Address`、
  `OrderItem.children[] → OrderItem`。
- **错误环**：纯别名环（`AliasA → AliasB → AliasA`，中间无结构出口）
  → 只把出错子树标记为 `kind:"error", code:"cycle"`，同级字段与其它根不受影响；
  无法解析的引用是 `code:"unresolved"`。

### 4. 构建时完全展开 vs 按节点惰性解析（同一内核）
`compute_children(spec, root, node_id, depth)` 是**唯一展开内核**：
- **惰性**：前端默认调用 `GET /children?id=<边序列>` 逐层取，结果按
  `(version,root,id,depth)` 缓存在 `node_cache`；
- **全量**：`build_full()` 递归调同一内核，`GET /tree` 使用，导出也使用。

节点 id 是从根出发、可稳定重放的边序列：`p~属性` / `i`（数组元素）/
`o~oneOf~0`（分支）/ `r~`（展开引用的合成边，记录深度消耗）。
惰性、全量、分享恢复走同一条代码路径，测试 `test_full_and_lazy_share_same_kernel`
保证逐节点完全一致。

| 维度 | 构建期全量展开 | 按节点惰性解析 |
|---|---|---|
| 首屏成本 | 一次构建整棵（深度受限时有限） | 只取根的直接孩子 |
| 大/递归 schema | 深度上限内全量，引用即停 | 点到哪展到哪，深度独立计 |
| 一致性 | 与惰性同一内核、同一 id 重放 | 同左（有 `node_cache` 复用） |
| 适用 | 导出、SEO/静态文档、离线产物 | 超大树交互、低权限只看局部 |

### 5. 可分享展开状态 + 访问权限
- `POST /api/share {vid,expanded,root,full,depth}` 返回 HMAC 签名令牌
  （`?share=…`），令牌载荷**绑定单个 vid**：只能读该不可变版本，
  发布新版本也不越权；过期/改密均 403。打开分享链接恢复根、深度、展开集合。
- ACL：`reader < editor < admin`，首次创建某规范的用户自动成为 admin；
  读接口接受 ACL 或有效分享令牌，写接口要求 editor+。

### 6. 参数变更 → 依赖示例复验，旧示例不冒充适配新版
- 每个版本对每个示例有一行 `example_validations`（含 `basis_version_id`）。
- 发布新版本时计算**参数级指纹 diff**（added/removed/changed/newly_required，
  指纹只含影响合法性的约束，deprecated/描述不参与）；
- 依据**反向索引**（`param_example_index`，含递归数组 `[]` 路径与前缀命中）
  求受影响示例 → 置 `needs_revalidation`；未受影响的沿用结论，
  但 `basis_version_id` 仍指旧版本（页面明确提示「未声明适配当前版本」）；
- `POST /versions/{id}/revalidate` 才按当前快照重新校验，`basis` 才更新到新版。

### 7. 同一 schema 快照
所有读取 URL 都带 `spec_version_id`，`raw` 快照不可变；
头部显示 `快照 #id · name@version · sha256`。
**表格（tree/children）、样例高亮（examples）、导出（export JSON/CSV）**
都从该版本构建，互不串版本。展开过程中切换版本：旧节点 id 对新版本重放
会返回 404（如 v2 删除了 cardNo），不会显示 v1 的数据。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/specs` | 规范与版本列表（含当前用户 access） |
| POST | `/api/specs/{name}/versions` | 发布版本（首个→admin；之后需 write） |
| GET | `/api/versions/{id}` | 版本元数据 + sha256 |
| GET | `/api/versions/{id}/roots` | 根 schema 列表 |
| GET | `/api/versions/{id}/tree?root&depth` | 构建期全量树 |
| GET | `/api/versions/{id}/children?root&id&depth` | 按节点惰性展开 |
| GET | `/api/versions/{id}/examples?root&path` | 参数→示例反查 + 三态 |
| POST | `/api/versions/{id}/examples` | 提交示例（立即按快照校验） |
| POST | `/api/versions/{id}/revalidate` | 待复验示例按当前快照复验 |
| GET | `/api/versions/{id}/diff` | 相对上版本参数级 diff |
| GET | `/api/versions/{id}/export?fmt=json\|csv` | 导出（与页面同源） |
| POST | `/api/share` / GET `/api/share/{token}` | 分享单版本只读 |

所有接口可加 `?share=<token>` 做只读分享访问；写接口需 `X-User-Id` 对应权限。

## 测试

```bash
python3 tests/test_acceptance.py   # 17 个：三态/同名字段/条件分支/递归/错误环/升级/切换
python3 tests/test_http.py         #  4 个：HTTP 快照同源、ACL/分享、版本切换、CSV
```

## 目录

```
schema_service/
  resolver.py    $ref 解析 + allOf/$ref 兄弟键深合并
  conditions.py  条件 AST（构建/求值/中文渲染）
  tree.py        展开内核（全量/惰性共用）、引用环判定、深度限制
  validate.py    三态校验、分支 oneOf/anyOf、依赖必填
  versions.py    版本落库、参数指纹 diff、受影响标记、复验
  examples.py    参数→示例反向索引、三态描述
  shares.py      HMAC 分享令牌 + 角色 ACL
  db.py / sql/   SQLite 连接与表结构
  server.py      标准库 HTTP API + 静态页
static/index.html  单页面前端（无构建步骤）
specs/order-v1.json, order-v2.json   覆盖全部验收点的演示规范
seed.py          建库与演示数据
```
