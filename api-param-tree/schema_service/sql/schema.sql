-- 规范版本、示例复验、参数 -> 示例反向索引、分享权限的持久化结构。
-- 所有读取都以 spec_version_id 为锚：版本行不可变（raw 原样保存），
-- 页面表格 / 样例高亮 / 导出因此天然同源。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spec_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_name      TEXT NOT NULL,
    version        TEXT NOT NULL,
    raw            TEXT NOT NULL,                 -- 不可变快照原文
    sha256         TEXT NOT NULL,
    diff_from_prev TEXT NOT NULL DEFAULT '{}',
    created_by     TEXT NOT NULL DEFAULT 'anonymous',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_spec_versions_name ON spec_versions(spec_name, id);

CREATE TABLE IF NOT EXISTS examples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_name  TEXT NOT NULL,
    root       TEXT NOT NULL,
    title      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'anonymous',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_examples_spec ON examples(spec_name);

-- 同一示例对每个规范版本都有一行校验结论。
-- basis_version_id 记录结论依据的版本：旧示例不会冒充已适配新版，
-- 新参数发布后相关示例先落 needs_revalidation，复验后才变 valid/invalid。
CREATE TABLE IF NOT EXISTS example_validations (
    example_id       INTEGER NOT NULL REFERENCES examples(id),
    spec_version_id  INTEGER NOT NULL REFERENCES spec_versions(id),
    status           TEXT NOT NULL,                 -- valid/invalid/needs_revalidation/unvalidated
    errors           TEXT NOT NULL DEFAULT '[]',
    basis_version_id INTEGER,
    validated_at     TEXT,
    PRIMARY KEY (example_id, spec_version_id)
);

-- 「参数 -> 示例」反向索引：param_path 形如 root:field.sub[]，按版本维护
CREATE TABLE IF NOT EXISTS param_example_index (
    spec_version_id INTEGER NOT NULL REFERENCES spec_versions(id),
    param_path      TEXT NOT NULL,
    example_id      INTEGER NOT NULL REFERENCES examples(id),
    PRIMARY KEY (spec_version_id, param_path, example_id)
);
CREATE INDEX IF NOT EXISTS idx_pei_example ON param_example_index(example_id);

CREATE TABLE IF NOT EXISTS permissions (
    user_id   TEXT NOT NULL,
    spec_name TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('reader','editor','admin')),
    PRIMARY KEY (user_id, spec_name)
);

-- 惰性展开结果按「版本 + 根 + 节点 + 深度」缓存；快照不可变故可永久缓存
CREATE TABLE IF NOT EXISTS node_cache (
    spec_version_id INTEGER NOT NULL,
    cache_key       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    PRIMARY KEY (spec_version_id, cache_key)
);
