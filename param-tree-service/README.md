# 参数树服务（param-tree-service）

为 API 文档生成「参数树」的参考实现。**仅依赖 Python 3 标准库**（含 SQLite），
前端为零构建静态页。设计依据见 [DESIGN.md](./DESIGN.md)。

## 解决的关键问题

- 参数按 **必填 / 条件必填 / 可选 / 废弃** 分组；条件必填是**谓词列表**，
  不压成全局布尔；同条件 required/forbidden 冲突显式报 `branch-conflict`。
- **默认值 / 缺省 / null** 三分（`defaulted | absent | null`）。
- 解析 `$ref`、`allOf/oneOf/anyOf/if-then-else/not`；以「是否跨过生产性边」
  区分 **合法自引用**（渲染为引用节点、每定义最多展开 3 层、全局深度 30）与
  **错误引用环**（纯别名/allOf 闭合，报 `ref-cycle`）；悬空引用报 `ref-dangling`。
- **惰性按节点解析**（默认）与构建时全量走查（仅填 SQL 目录/反向索引，带节点预算）
  并存；节点句柄带快照哈希，展开态可分享，**每次展开都做权限校验**。
- 发布新版本做参数指纹差异；新增必填字段与被触达示例进入**复验队列**（`pending`），
  旧示例不会显示成「已适配新版」。
- 会话创建即固定**不可变快照**；表格、示例高亮、导出取自同一快照；展开中切换版本
  返回 `kept/changed/removed` 映射，旧句柄立即失效。

## 目录

```
ptsvc/
  model.py      节点 / 需求谓词 / 路径状态 / 上限常量
  resolver.py   $ref、组合关键字、递归与错环判定、惰性句柄
  validator.py  示例校验（条件求值、默认/缺省/null、递归载荷）
  store.py      SQLite：快照/版本/参数目录/示例反向索引/复验队列/分享/权限
  service.py    发布与影响分析、会话、分享、版本切换、导出
  server.py     标准库 HTTP API + 静态页托管
web/            index.html / app.js（分组树、JSON 高亮、版本/分享/导出）
tests/          验收测试（unittest）
seed_demo.py    演示数据
```

## 运行

```bash
cd param-tree-service
python3 seed_demo.py demo.db          # 写入演示数据
python3 -m ptsvc.server --db demo.db --port 8077
# 浏览器打开 http://127.0.0.1:8077/  （主体可填 alice / auditor）
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/admin/apis/:api/publish` | 发布 `{version, schema}`，返回快照与复验影响 |
| POST | `/api/admin/examples` | 上传示例（立即按最新快照校验） |
| POST | `/api/admin/examples/:id/revalidate` | 对指定快照复验 |
| POST | `/api/sessions` | `{api, version?, principal}` 固定快照 |
| GET  | `/api/sessions/:id/tree?nodeId=` | 根一层 / 节点浅层（惰性） |
| POST | `/api/sessions/:id/expand` `/collapse` | 展开（鉴权+快照校验）/折叠 |
| POST | `/api/sessions/:id/highlight` | 示例逐路径状态（同会话快照，按权限过滤） |
| POST | `/api/sessions/:id/switch-version` | 版本切换与展开态映射 |
| POST | `/api/sessions/:id/share` `/from-share` | 分享展开态（接收方重新鉴权） |
| POST | `/api/sessions/:id/export` | 树+示例+快照元数据整包 |

错误码：`ref-dangling / ref-cycle / branch-conflict / forbidden-node /
snapshot-mismatch(409) / not-found(404)`。
