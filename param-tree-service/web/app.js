const App = (() => {
  const state = { sessionId: null, snapshotId: null, tree: null,
                nodes: new Map(), highlight: null };

  const $ = (id) => document.getElementById(id);
  async function api(path, body, method = "POST") {
    const r = await fetch("/api" + path, {
      method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw Object.assign(new Error(j.error?.message || "error"),
                                   { code: j.error?.code });
    return j;
  }
  function toast(msg) {
    const t = $("toast"); t.textContent = msg; t.style.display = "block";
    clearTimeout(toast._t); toast._t = setTimeout(() => t.style.display = "none", 2600);
  }

  async function open() {
    const api_name = $("api").value.trim();
    const version = $("version").value || null;
    const principal = $("principal").value.trim() || "anon";
    const s = await api("/sessions", { api: api_name, version, principal });
    state.sessionId = s.session_id; state.snapshotId = s.snapshot_id;
    await refreshVersions();
    await loadRoot();
    await loadExamples();
  }

  async function refreshVersions(selectVersion) {
    const api_name = $("api").value.trim();
    const j = await fetch(`/api/admin/apis/${encodeURIComponent(api_name)}/versions`)
                .then(r => r.json());
    const sel = $("version"); sel.innerHTML = "";
    for (const v of (j.versions || [])) {
      const o = document.createElement("option");
      o.value = v.version; o.textContent = v.version + (v.is_latest ? " (latest)" : "");
      o.dataset.snap = v.snapshot_id;
      if (v.snapshot_id === state.snapshotId) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = switchVersion;
  }

  async function loadRoot() {
    const j = await api(`/sessions/${state.sessionId}/tree`, undefined, "GET");
    state.snapshotId = j.snapshot_id;
    $("snap").textContent = "snapshot " + j.snapshot_id;
    state.nodes.clear();
    state.tree = j.tree; indexNode(state.tree);
    renderTree();
  }

  function indexNode(n) {
    state.nodes.set(n.node_id, n);
    (n.children || []).forEach(indexNode);
  }

  async function expand(n) {
    const fresh = await api(`/sessions/${state.sessionId}/expand`,
                            { node_id: n.node_id });
    // 服务端返回该节点物化层；挂到缓存
    const parent = state.nodes.get(n.node_id);
    Object.assign(parent, fresh);
    parent.children.forEach(indexNode);
    renderTree();
    if (state.highlight) renderHighlight();
  }
  function collapse(n) {
    n.children = []; n.expanded = false;
    api(`/sessions/${state.sessionId}/collapse`, { node_id: n.node_id });
    renderTree();
  }

  function groupOf(n) { return n.group; }

  function renderTree() {
    const root = state.tree; if (!root) return;
    const groups = { required: [], conditional: [], optional: [], deprecated: [], branch: [] };
    for (const c of root.children || []) groups[c.group || groupOf(c)].push(c);
    const titles = { required: "必填", conditional: "条件必填（点击查看条件）",
                     optional: "可选", deprecated: "废弃（deprecated）",
                     branch: "条件分支（oneOf / anyOf / if-then）" };
    const el = $("tree"); el.innerHTML = "";
    for (const key of ["required", "conditional", "optional", "deprecated", "branch"]) {
      if (!groups[key].length) continue;
      const t = document.createElement("div");
      t.className = "group-title " + (key === "branch" ? "conditional" : key);
      t.textContent = `${titles[key]} · ${groups[key].length}`;
      el.appendChild(t);
      groups[key].forEach(n => el.appendChild(renderRow(n, 0)));
    }
    // 分支伪节点（oneOf/anyOf/if）挂在对象节点下，随 children 展示
  }

  function renderRow(n, depth) {
    const wrap = document.createElement("div");
    const row = document.createElement("div");
    row.className = "row";
    const hasChildren = n.expandable || (n.children && n.children.length);
    const caret = document.createElement("span");
    caret.className = "caret";
    const expandedNow = n.children && n.children.length > 0;
    caret.textContent = hasChildren ? (expandedNow ? "▾" : "▸") : "";
    caret.onclick = (e) => { e.stopPropagation(); toggle(n); };
    row.appendChild(caret);

    const label = document.createElement("span");
    const kindIcon = n.kind === "ref" ? "↻ " : n.kind === "branch" ? "⑂ " : "";
    label.innerHTML = `<span class="name">${kindIcon}${escapeHtml(n.name)}</span>` +
      (n.value_type ? ` <span class="type">${n.value_type}</span>` : "");
    row.appendChild(label);

    const badges = document.createElement("span"); badges.className = "badges";
    const addBadge = (txt, cls) => {
      const b = document.createElement("span"); b.className = "badge " + cls;
      b.textContent = txt; badges.appendChild(b);
    };
    if (n.kind === "ref") addBadge("递归引用 " + (n.ref_target || ""), "rec");
    if (n.group === "required" && !n.deprecated) addBadge("必填", "req");
    if (n.group === "conditional") addBadge("条件", "cond");
    if (n.deprecated) addBadge("deprecated", "dep");
    if (n.has_default) addBadge("default " + JSON.stringify(n.default), "");
    if (n.nullable) addBadge("nullable", "");
    for (const d of n.diagnostics || []) {
      const cls = d.code === "forbidden-node" ? "lock"
        : d.code === "ref-cycle" ? "err" : "cond";
      addBadge(d.code === "forbidden-node" ? "受限" : d.code, cls);
      if (d.code === "forbidden-node") row.style.opacity = .55;
    }
    row.appendChild(badges);
    wrap.appendChild(row);

    // 条件必填谓词
    const preds = (n.requiredness || []).filter(r => r.when);
    if (preds.length) {
      const pd = document.createElement("div"); pd.className = "preds";
      pd.textContent = preds.map(r =>
        `${r.require === "forbidden" ? "禁止" : "必填"} 当 ${r.when}`).join("；");
      wrap.appendChild(pd);
    }
    if (n.description) {
      const dd = document.createElement("div"); dd.className = "desc";
      dd.textContent = n.description; wrap.appendChild(dd);
    }

    if (expandedNow) {
      const cwrap = document.createElement("div"); cwrap.className = "children";
      n.children.forEach(ch => cwrap.appendChild(renderRow(ch, depth + 1)));
      wrap.appendChild(cwrap);
    }
    n._el = wrap;
    return wrap;
  }

  async function toggle(n) {
    if (!(n.expandable || n.children)) return;
    if (n.children && n.children.length) collapse(n);
    else await expand(n);
  }

  // ---------- 示例 ----------
  async function loadExamples() {
    // 导出包含示例清单（轻量做法）
    const bundle = await api(`/sessions/${state.sessionId}/export`, {});
    state.bundle = bundle;
    const sel = $("exampleSel"); sel.innerHTML = "";
    for (const ex of bundle.examples) {
      const o = document.createElement("option");
      o.value = ex.example_id;
      o.textContent = `${ex.name} · ${ex.recorded_state}`;
      sel.appendChild(o);
    }
    if (bundle.examples.length) { sel.selectedIndex = 0; await highlight(); }
    else $("json").textContent = "（暂无示例）";
  }

  async function highlight() {
    const eid = $("exampleSel").value;
    const j = await api(`/sessions/${state.sessionId}/highlight`, { example_id: eid });
    state.highlight = j;
    const ex = (state.bundle.examples.find(x => x.example_id === eid) || {});
    $("exState").innerHTML = statePill(j.recorded_state) +
      (j.needs_revalidation ? ' <span class="pill pending">待复验（旧示例未适配新版）</span>' : "");
    renderHighlight();
  }

  function renderHighlight() {
    const eid = $("exampleSel").value;
    const ex = (state.bundle.examples.find(x => x.example_id === eid) || {});
    const paths = state.highlight?.paths || {};
    $("json").innerHTML = renderJson(ex.payload, "$", paths);
  }

  function statePill(s) {
    return `<span class="pill ${s}">${s}</span>`;
  }

  // 把 JSON 逐键着色，状态来自同快照高亮 paths
  function renderJson(value, path, paths) {
    const st = paths[path];
    const cls = st ? `s-${st}` : "";
    const title = st ? `title="${st} ${path}"` : "";
    if (value === null) return `<span class="json-row ${cls}" ${title}>null</span>`;
    if (typeof value !== "object") {
      return `<span class="json-row ${cls}" ${title}>${escapeHtml(JSON.stringify(value))}</span>`;
    }
    const entries = Array.isArray(value)
      ? value.map((v, i) => [i, v, `${path}[${i}]`])
      : Object.keys(value).map(k => [k, value[k], path === "$" ? k : `${path}.${k}`]);
    let out = Array.isArray(value) ? "[\n" : "{\n";
    out += entries.map(([k, v, p]) => {
      const keyHtml = Array.isArray(value) ? "" :
        `<span class="json-key">${escapeHtml(String(k))}</span>: `;
      return "  " + keyHtml + renderJson(v, p, paths);
    }).join(",\n");
    out += "\n" + (Array.isArray(value) ? "]" : "}");
    // 对整个对象块的缺失/默认提示（子路径在树中可见）
    return `<span class="${cls}" ${title}>${out}</span>`;
  }

  // ---------- 版本切换 ----------
  async function switchVersion() {
    const version = $("version").value;
    const r = await api(`/sessions/${state.sessionId}/switch-version`, { version });
    state.snapshotId = r.snapshot_id;
    $("snap").textContent = "snapshot " + r.snapshot_id;
    const info = [];
    if (r.changed.length) info.push(`语义变化: ${r.changed.join(", ")}`);
    if (r.removed.length) info.push(`已移除: ${r.removed.join(", ")}`);
    $("switchInfo").textContent = info.join("；");
    toast(`已切换到 ${version}`);
    await loadRoot();
    await loadExamples();
  }

  async function revalidate() {
    const eid = $("exampleSel").value;
    await api("/admin/examples/" + eid + "/revalidate",
              { snapshot_id: state.snapshotId });
    toast("复验完成");
    await loadExamples(); await highlight();
  }

  async function share() {
    const r = await api(`/sessions/${state.sessionId}/share`, {});
    toast("分享令牌: " + r.share_id);
  }

  async function exportBundle() {
    const b = await api(`/sessions/${state.sessionId}/export`, {});
    const blob = new Blob([JSON.stringify(b, null, 2)],
                         { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `params-${$("api").value}-${b.snapshot_id}.json`;
    a.click();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  return { open, expand, collapse, share, exportBundle, highlight,
           revalidate, toggle };
})();
