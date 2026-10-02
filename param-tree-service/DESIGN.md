# API 参数树服务 · 设计文档

## 1. 目标与范围

为 API 文档提供一棵「参数树」，并解决以下在普通 JSON Schema 渲染器中容易被压平、
导致语义丢失的问题：

1. 参数按 **必填 / 可选 / 废弃** 分组展开；必填性可能依赖分支（oneOf / anyOf /
   if-then-else / allOf 组合），**条件必填不得压成全局布尔值**。
2. **默认值（default）、缺省（omitted/absent）、null** 是三种不同语义，分别建模。
3. 解析 `$ref` 与组合关键字；**合法递归**（如树节点 `children` 指向自身）必须以
   「引用节点」呈现并限制展开深度；**错误引用环**（如 A→B→A 的纯别名环）与合法自
   引用不能一律报错，也不能一律当作无限展开。
4. **构建时完全展开** 与 **按节点惰性解析** 两种模式的取舍；惰性解析需支持
   **可分享的展开状态** 与每次展开的 **访问权限校验**。
5. 参数变更后，覆盖到该参数路径的示例进入 **复验队列**；旧示例不得显示为
   「已适配新版」。
6. 页面表格、样例高亮、导出 **必须取自同一个 schema 快照**；展开过程中切换版本
   有明确定义的迁移语义。

参考实现位于 `param-tree-service/`，仅依赖 Python 标准库（含 SQLite）与静态 HTML。

---

## 2. 总体架构

```
                ┌──────────────────────────────────────────────┐
                │                  Web 前端                     │
                │  参数分组表格 │ 树展开 │ 示例高亮 │ 导出/分享   │
                └───────────────┬──────────────────────────────┘
                                │ HTTP (JSON)
                ┌───────────────▼──────────────────────────────┐
                │              ViewerSession（会话层）           │
                │  快照固定(snapshotId) · 展开集合 · 权限主体    │
                │  分享令牌 · 版本切换迁移 · 导出包              │
                └───────┬───────────────────────┬──────────────┘
                        │                       │
              ┌─────────▼─────────┐   ┌─────────▼──────────┐
              │ Resolver（解析层） │   │ ExampleValidator    │
              │ $ref/组合/递归     │   │ 条件求值/路径状态    │
              │ 条件必填/惰性句柄   │   │ 默认 vs 缺省 vs null│
              └─────────┬─────────┘   └─────────┬──────────┘
                        │                       │
                ┌───────▼───────────────────────▼──────┐
                │        Store（SQLite + 内容寻址）      │
                │ snapshots/spec_versions/params        │
                │ examples/example_paths/revalidation   │
                │ shares/access_policies                │
                └───────────────────────────────────────┘
```

### 2.1 不可变快照（内容寻址）

- 发布 `POST /admin/apis/{name}/publish`：服务端对 schema 文档做 JCS 风格规范化
  JSON 后取 SHA-256，得到 `snapshotId`（`sha256:` 前缀 + 16 位 hex）。
- 快照与版本号绑定（`spec_versions`），**snapshot 一经写入不可变**；任何节点句柄、
  分享链接、导出包都带 `snapshotId`。
- 会话创建即固定快照。前端表格、高亮、导出在一次请求链路里使用同一个快照对象，
  杜绝「渲染一半读到新版本」。

---

## 3. 参数模型（不丢语义的表示）

### 3.1 参数身份：路径而不是字段名

参数的全局身份是 **参数路径 ParamPath**，字段名只在同级内唯一：

- 路径段：对象属性 `foo`、数组项 `[]`；
- 同名字段不同路径是不同参数：`billing.address.zip` 与 `shipping.address.zip`
  在 `params` 表中主键不同，互不覆盖（验收场景 1）。

节点的**惰性句柄 NodeHandle**（可编码为不透明 ID）：

```jsonc
{
  "snap": "sha256:9f2c...",      // 快照隔离：旧句柄不能打到新快照
  "loc":  ["properties","address","properties","zip"], // 结构位置（指针段）
  "name": "zip",                 // 显示名
  "kind": "scalar",              // object | array | scalar | ref | branch | error
  "depth": 2,                    // 已展开结构深度
  "chain": [["#/$defs/Node", 1]],// ref 激活栈：[ref, productiveLevel]
  "occ":  {"#/$defs/Node": 1},   // 每个定义在当前路径上的出现次数
  "bctx": [ {"id":"b0","cond":"kind == 'person'"} ], // 分支上下文
  "vi":   null                   // 变体 key（oneOf 内的字段）
}
```

`chain/occ/bctx` 都在句柄中，因此惰性展开是**无状态可重放**的，服务端无需为每个
节点缓存遍历栈；同时句柄带快照哈希，天然防止跨版本重放。

### 3.2 必填性：谓词集合，而非布尔

字段 `requiredness` 是一个列表，每项是一个**需求谓词**：

```jsonc
[
  { "require": "required", "when": null,                 "source": "#/properties/age" },
  { "require": "required", "when": "kind == 'person'",   "source": "b0" },
  { "require": "forbidden","when": "kind == 'company'",  "source": "b1" }
]
```

- `when == null`：无条件；`required | optional | forbidden`。
- 非 null：条件需求，由 if/then/else 或 oneOf/anyOf 变体派生。
- **同一条件下出现互相矛盾的需求**（required 与 forbidden 并存）不做静默合并，
  挂 `branch-conflict` 诊断，UI 显示冲突徽章（验收场景 2）。
- 分组规则（同一字段的最终展示分组取最严格的存在性，再受废弃态覆盖）：
  - 存在无条件 required 且无无条件 forbidden → **必填**；
  - 其余若有条件 required/forbidden → 必填组但带「条件」标记，
    点击可展开谓词（**不进入普通可选**）；
  - 仅无条件 optional → 可选；
  - 任何贡献声明 `deprecated: true` → **废弃**组（保留必填/条件徽标，
    废弃只影响展示分组，不改语义）。

### 3.3 默认值 / 缺省 / null 三分

| 概念 | 模型表示 | 示例载荷 | 高亮/校验 |
|---|---|---|---|
| 默认值 | schema `default: 0`，节点 `has_default=true, default=0` | 字段不出现 | 显示「缺省→默认 0」，校验默认视为通过 |
| 缺省（omitted） | 无 `default`，字段未出现 | 字段不出现 | 状态 `absent`；必填时报 missing |
| null | JSON `null`；类型允许 `null`（type 数组含 "null" 或 enum 含 null） | `"x": null` | 状态 `null`，是一个显式值，不是缺省 |

三者在示例高亮接口中分别返回 `defaulted | absent | null | present | missing`，
禁止把 `null` 序列化成「未填」或把缺省渲染成 null。

### 3.4 组合关键字语义

- **allOf**：合取。属性合并、required 合取；`default/type/deprecated` 按
  「首定义优先」，冲突挂诊断。allOf 中的 `$ref` 是**非生产性边**（只是引用展开）。
- **oneOf / anyOf**：**不合并成扁平对象**。生成一个 `branch` 伪节点，每个变体
  一个分支子节点；所有变体公共属性才上提为基础属性。分支条件启发式从
  `const/enum` 判别字段派生（如 `type == 'person'`），派生不出则显示
  `variant N`。
- **if/then/else**：if 的 schema 被编译为谓词（支持 const/enum/type/required 与
  简单合取）；then/else 内 required 转为**条件需求谓词**挂到当前对象字段上，
  属性体挂到 `branch` 伪节点供查看。
- **not / false schema**：贡献 forbidden 需求；`false` 本身表示「无合法值」。

---

## 4. 引用、递归与错误环（核心算法）

### 4.1 生产性边

把解析过程看成在「定义图」上的游走：

- **生产性边（productive）**：沿真实结构层级下行——对象属性、数组 items；
- **非生产性边**：裸 `$ref` 跳转、allOf 合取展开。

### 4.2 判定规则（不搞一刀切）

解析器为当前遍历路径维护两个量：

- `occ[D]`：定义 D 在当前路径上的出现次数；
- `chain`：激活引用栈，元素 `[ref, productiveLevel]`。

每次沿 `$ref` 跳转时：

1. **目标缺失** → `error` 节点（诊断 `ref-dangling`），不抛异常、不中断全树。
2. 目标在 `chain` 中已激活：
   - 距上次出现之间存在**至少一条生产性边**（例如 Node→properties/children
     （生产性）→items 里的 $ref Node）→ **合法递归**：输出 `ref` 引用节点
     （显示「↻ #/$defs/Node · 递归，可再展开 n 层」）。点击展开时 `occ[D]+1`，
     超过每定义上限 `MAX_REF_EXPAND`（默认 3）即只显示引用桩，不再可展开。
   - 中间**没有任何生产性边**（A 裸 ref B，B 裸 ref A；或 A allOf→ref B，
     B allOf→ref A 的纯别名环）→ **错误环**：输出 `error` 节点
     （诊断 `ref-cycle`，并给出引用链）。这是不可展示的空结构，而不是递归数据。
3. 另设全局结构深度上限（默认 30）兜底，防止混合形态爆炸。

这样「TreeNode.children → TreeNode」走的是属性边（生产性），显示为受控深度的
引用节点；「A=B 的别名环」无非生产性边，直接报环；两者路径完全不同
（验收场景 3）。

### 4.3 两种展开模式的比较

| 维度 | 构建时完全展开 | 按节点惰性解析（默认） |
|---|---|---|
| 发布成本 | 递归全量物化，深度/广度受限 | O(1) 建快照，仅做索引走查 |
| 深层递归 | 必须提前截断，内容可能巨大 | 用户点击才展开，深度逐跳授权 |
| 权限 | 发布时一次性过滤，粒度粗 | **每次 expand 都做权限判定**，可按子树收敛 |
| 分享 | 只能分享静态结果 | 分享展开状态 + 快照 ID，接收者重放 |
| 一致性 | 天然单快照 | 句柄带快照哈希，同样单快照 |
| 索引 | 直接全量 | 发布时做「安全上限内的索引走查」填充 SQL 目录与反向索引 |

实现采用**混合策略**：渲染树惰性；发布时另跑一个带安全上限（节点数上限）的
索引走查，只写目录与反向索引（不保留物化树）。

---

## 5. 会话、分享与权限

### 5.1 ViewerSession

`POST /sessions {api, version?, principal, scope}` 创建会话：

- 解析/固定 `snapshotId`（版本缺省取最新已发布版）；
- `expanded`：展开节点句柄集合；`selection`：当前选中路径；
- 之后所有 `expand/collapse/tree/highlight/export` 请求都带 `sessionId`，
  服务端校验句柄中的 `snap == session.snapshotId`，不一致返回 `snapshot-mismatch`。

### 5.2 展开状态可分享

- `POST /sessions/{id}/share` → 生成 `shareId`，内容 =
  `{snapshotId, rootApi, expanded[], selection, scopeHint}`，存入 `shares` 表。
- 接收者 `POST /sessions/from-share {shareId, principal}`：服务端**按新主体重新
  做权限校验**（分享不传递权限），无权展开的节点从展开集合中剔除并返回
  `dropped[]`；快照仍然固定为分享时的快照。

### 5.3 访问权限校验

- `access_policies` 表：`(snapshotId, pathPattern, principal, effect)`，
  pattern 支持 `params.foo.*`。
- 校验时机：
  1. 会话创建：根路径可读检查；
  2. **每次 expand 节点**：检查该节点路径及其直接子节点（子树在展开时逐个校验，
     这是惰性模式相对全量展开的核心收益）；
  3. 高亮/导出：统一过滤，不可见字段从树与示例路径中一并移除（不会借示例泄露）。
- 无权节点以 `ref` 同构的「受限节点」呈现（`kind=error, code=forbidden-node`），
  不区分「不存在」与「无权」之外的额外信息。

---

## 6. 存储模型（SQLite）

```sql
spec_versions(api_name, version, snapshot_id, published_at, published_by,
              is_latest, PRIMARY KEY(api_name, version))
snapshots(snapshot_id PK, created_at, canonical_json, root_pointer)
params(snapshot_id, param_path PK/split, name, depth, kind, requiredness_json,
       has_default, default_json, nullable, deprecated, description, ref_target,
       UNIQUE(snapshot_id, param_path))
examples(example_id PK, api_name, name, payload_json, created_at)
example_versions(example_id, snapshot_id, state, checked_at, issues_json,
                 PRIMARY KEY(example_id, snapshot_id))   -- passed/failed/pending/stale
example_paths(example_id, snapshot_id, param_path, state,   -- 反向索引
              PK(example_id, snapshot_id, param_path))
revalidation_queue(snapshot_id, example_id, reason, created_at,
                   PK(snapshot_id, example_id))
shares(share_id PK, snapshot_id, api_name, state_json, scope_hint, created_by, created_at)
access_policies(id, api_name, path_pattern, principal, effect)
sessions 仅内存（重启失效；分享态已持久化，可重建）
```

「参数 → 示例」的反向索引即 `example_paths`：给定 param_path 可直接查出覆盖它的
全部示例；这也是变更影响分析的输入。

### 6.1 发布、影响分析与复验

发布新版本快照时：

1. 写入 `snapshots / spec_versions`，翻转 `is_latest`（事务内）。
2. 新旧快照的 `params` 做差异，得到 `changed_paths`（schema 指纹变化，含新增/
   删除/必填性/默认/类型变化）。
3. 反查旧快照 `example_paths`：凡覆盖任一 changed_path 的示例，在新快照下写
   `example_versions.state = 'pending'` 并入 `revalidation_queue`（reason 记录
   触达路径）；未触达的示例直接用校验器重跑并置 passed/failed。
4. **旧快照行保持原样**。旧版本页面上示例仍按旧行显示；新版本在复验完成前一律
   显示「待复验」徽标，不存在「旧示例显示为已适配新版」的路径（验收场景 4）。
5. `POST /admin/examples/{id}/revalidate?version=` 消费队列；校验器输出
   passed/failed 与逐路径状态。

---

## 7. 示例校验器与高亮

校验器复用 Resolver 的去引用/组合展开（同一套递归判定），对载荷自顶向下求值：

- 类型、enum、required（**按 if/oneOf 实际命中的分支**求值条件必填）、
  forbidden、additionalProperties；
- 递归载荷用「同一路径上同一 def + 载荷身份」做递归保护；
- 输出：`state(passed/failed)`、`issues[]`、`paths{paramPath: state}`，
  state ∈ `present | null | defaulted | absent | missing | invalid`。

高亮接口 `POST /sessions/{id}/highlight {exampleId}`：
从**会话快照**解析、跑校验、按权限过滤后返回路径状态；前端树与右侧 JSON 同步
着色。导出接口 `POST /sessions/{id}/export` 把树（按当前展开集合）、示例及其
校验结果、快照元数据打成一个 JSON 包，包内冗余 `snapshotId` 供消费者核对。

---

## 8. 展开过程中切换版本

`POST /sessions/{id}/switch-version {targetVersion}`：

1. 定位目标快照；对当前展开集合中的每个 ParamPath 做映射：
   - 路径在新快照存在且节点指纹兼容 → 保留展开；
   - 存在但语义变化（类型/必填性/默认变化）→ 保留但标 `changed`，前端高亮提示；
   - 已删除 → 放入 `removed[]` 并从展开集合剔除；
   - 新增路径（如递归层级因定义调整而出入）不自动展开。
2. 会话 `snapshotId` 原子切换，旧句柄立即失效（再提交返回 snapshot-mismatch，
   前端用新句柄重渲染）；选择路径同理映射（验收场景 5）。
3. 切换后树、示例高亮徽标（示例对新快照的 pending/passed/failed）、导出全部来自
   新快照；不存在「树是新版、示例状态还是旧版」的混合。

---

## 9. HTTP API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/apis/:api/publish` | 发布版本（version + schema JSON）→ snapshotId、变更与入队结果 |
| GET  | `/admin/apis/:api/versions` | 版本列表 |
| POST | `/admin/examples` | 上传示例 |
| POST | `/admin/examples/:id/revalidate` | 对指定快照复验 |
| POST | `/sessions` | 创建会话（固定快照、主体、权限） |
| POST | `/sessions/from-share` | 按分享令牌重建会话（重新鉴权） |
| GET  | `/sessions/:id/tree?nodeId=` | 取根/某节点的一层（惰性） |
| POST | `/sessions/:id/expand` | 展开节点（权限校验 + 快照校验） |
| POST | `/sessions/:id/collapse` | 折叠 |
| POST | `/sessions/:id/select` | 选中参数 |
| POST | `/sessions/:id/highlight` | 示例逐路径高亮（同快照） |
| POST | `/sessions/:id/switch-version` | 版本切换与展开态迁移 |
| POST | `/sessions/:id/share` | 生成分享令牌 |
| POST | `/sessions/:id/export` | 导出整包（树/示例/快照元数据） |

错误统一 `{error: {code, message, detail?}}`，关键 code：
`ref-dangling / ref-cycle / branch-conflict / forbidden-node /
snapshot-mismatch / node-not-found / validation-failed`。

---

## 10. 验收场景与测试映射

| # | 场景 | 测试 |
|---|---|---|
| 1 | 同名字段不同路径（billing/shipping address.zip）不互相覆盖 | `test_same_name_different_paths` |
| 2 | 条件分支冲突（kind=person 时 ssn 必填，kind=company 时 ssn 禁止，错误 schema 同条件矛盾） | `test_conditional_required` / `test_branch_conflict` |
| 3 | 递归对象（TreeNode 自引用）= 引用节点 + 深度上限；A↔B 纯别名环 = ref-cycle | `test_legal_recursion` / `test_error_cycle` |
| 4 | 规范升级：受影响示例 pending 且显示「待复验」，未受影响直接重跑；复验后才 passed | `test_spec_upgrade_revalidation` |
| 5 | 展开中切换版本：保留/changed/removed 映射，句柄失效，三处视图同一快照 | `test_version_switch_during_expand` / `test_snapshot_consistency` |
| 附 | 默认/缺省/null 三分 | `test_default_absent_null` |
| 附 | 权限：子树不可见且无法借示例泄露；分享不传递权限 | `test_permissions_and_share` |
