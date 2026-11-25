(() => {
  "use strict";

  const TOKEN_KEY = "vestibule_ui_token";
  const TOKEN_RE = /^[\x21-\x7e]+$/;
  const NO_END_TOOLTIP = "still running, the server stopped, or the record was lost";
  const WHOLE_SANDBOX_NOTE = "counts the whole sandbox, including its setup and file cache";
  const SVG_NS = "http://www.w3.org/2000/svg";

  const state = {
    token: "",
    tokenGen: 0,
    runsSeq: 0,
    wsSeq: 0,
    lastDetailJson: null,
    view: "runs",
    paused: false,
    pollTimer: null,
    meta: null,
    runsData: null,
    selectedKey: null,
    filter: "all",
    q: "",
    limit: "200",
    wsPath: ".",
  };

  const els = {
    versionBadge: document.getElementById("version-badge"),
    tabRuns: document.getElementById("tab-runs"),
    tabWorkspace: document.getElementById("tab-workspace"),
    statusDot: document.getElementById("status-dot"),
    statusLabel: document.getElementById("status-label"),
    togglePoll: document.getElementById("toggle-poll"),
    tokenBanner: document.getElementById("token-banner"),
    tokenMessage: document.getElementById("token-message"),
    tokenForm: document.getElementById("token-form"),
    tokenInput: document.getElementById("token-input"),
    warningBanner: document.getElementById("warning-banner"),
    viewRuns: document.getElementById("view-runs"),
    viewWorkspace: document.getElementById("view-workspace"),
    summaryPills: document.getElementById("summary-pills"),
    statWall: document.getElementById("stat-wall"),
    statMem: document.getElementById("stat-mem"),
    statMemNote: document.getElementById("stat-mem-note"),
    sparklineBox: document.getElementById("sparkline-box"),
    statSparkNote: document.getElementById("stat-spark-note"),
    filterSelect: document.getElementById("filter-select"),
    searchInput: document.getElementById("search-input"),
    limitSelect: document.getElementById("limit-select"),
    scopeNote: document.getElementById("scope-note"),
    runsMetaNote: document.getElementById("runs-meta-note"),
    runsList: document.getElementById("runs-list"),
    runDetail: document.getElementById("run-detail"),
    wsBreadcrumbs: document.getElementById("ws-breadcrumbs"),
    wsRefresh: document.getElementById("ws-refresh"),
    wsMetaNote: document.getElementById("ws-meta-note"),
    wsBanner: document.getElementById("ws-banner"),
    wsBody: document.getElementById("ws-body"),
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function setStatus(kind, label) {
    els.statusDot.className = "status-dot is-" + kind;
    els.statusLabel.textContent = label;
  }

  function initToken() {
    const hash = window.location.hash ? window.location.hash.slice(1) : "";
    if (hash) {
      if (TOKEN_RE.test(hash)) {
        state.token = hash;
        try {
          window.sessionStorage.setItem(TOKEN_KEY, hash);
        } catch (_err) {
          // sessionStorage may be blocked; keep in memory
        }
      }
      try {
        window.history.replaceState(null, "", "/");
      } catch (_err) {
        // ignore replaceState failure
      }
      if (state.token) return;
    }
    try {
      const saved = window.sessionStorage.getItem(TOKEN_KEY) || "";
      state.token = TOKEN_RE.test(saved) ? saved : "";
    } catch (_err) {
      state.token = "";
    }
  }

  function requireToken(message) {
    state.tokenGen += 1;
    state.token = "";
    state.meta = null;
    try {
      window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (_err) {
      // ignore
    }
    stopPolling();
    els.tokenMessage.textContent = message;
    els.tokenBanner.classList.remove("is-hidden");
    setStatus("error", "Auth needed");
  }

  async function apiGet(path, params, reqTokenGen) {
    if (!state.token) {
      requireToken("Session token required. Paste the token from the URL printed by vestibule-mcp ui.");
      return null;
    }
    let url = path;
    if (params) {
      const qs = new URLSearchParams(params);
      url += "?" + qs.toString();
    }
    const resp = await window.fetch(url, {
      method: "GET",
      headers: { "X-Vestibule-Token": state.token },
      cache: "no-store",
    });
    if (resp.status === 403) {
      if (reqTokenGen === state.tokenGen) {
        requireToken("Session token rejected (403). If vestibule-mcp ui restarted, paste the new token.");
      }
      return null;
    }
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status);
    }
    return resp.json();
  }

  function callKey(call) {
    return [call.file, call.ts, call.id, call.has_start ? "s" : "e"].join("|");
  }

  function formatShortTs(ts) {
    if (!ts) return "—";
    const t = ts.indexOf("T");
    if (t === -1) return ts;
    return ts.slice(t + 1, t + 9);
  }

  function elapsedSince(ts) {
    const parsed = Date.parse(ts);
    if (!Number.isFinite(parsed)) return "";
    const diffSec = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
    if (diffSec < 60) return diffSec + "s";
    if (diffSec < 3600) return Math.floor(diffSec / 60) + "m " + (diffSec % 60) + "s";
    return Math.floor(diffSec / 3600) + "h " + Math.floor((diffSec % 3600) / 60) + "m";
  }

  function firstCodeLine(call) {
    if (!call.has_start && call.has_end) return "(no start record found)";
    if (!call.has_start && !call.has_end) return call.reason || "(blocked)";
    if (!call.code) return call.reason || "(blank)";
    const lines = call.code.split(/\r?\n/);
    for (const line of lines) {
      if (line.trim()) return line.trim();
    }
    return "(blank)";
  }

  function isolationBadgeClass(iso) {
    if (iso === "native") return "badge badge-ok";
    if (iso === "container") return "badge badge-info";
    if (typeof iso === "string" && iso.endsWith("-degraded")) return "badge badge-warn";
    if (iso === "none") return "badge badge-danger";
    return "badge badge-muted";
  }

  function outcomeBadge(call) {
    if (call.has_start && !call.has_end) {
      const age = elapsedSince(call.ts);
      const label = age ? "no end (" + age + ")" : "no end";
      const badge = el("span", "badge badge-warn", label);
      badge.title = NO_END_TOOLTIP;
      return badge;
    }
    const out = call.outcome || "unknown";
    let cls = "badge badge-muted";
    if (out === "ran") cls = call.exit_code === 0 ? "badge badge-ok" : "badge badge-warn";
    else if (out === "timed_out" || out === "refused" || out === "failed") cls = "badge badge-danger";
    else if (out === "cancelled" || out === "not_run") cls = "badge badge-warn";
    return el("span", cls, out);
  }

  function median(values) {
    if (!values.length) return null;
    const sorted = values.slice().sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    if (sorted.length % 2 === 1) return sorted[mid];
    return (sorted[mid - 1] + sorted[mid]) / 2;
  }

  function renderSparkline(calls) {
    clearNode(els.sparklineBox);
    const pts = [];
    for (let i = calls.length - 1; i >= 0; i--) {
      const w = calls[i].wall_ms;
      if (typeof w === "number" && Number.isFinite(w)) pts.push(w);
    }
    const recent = pts.slice(-40);
    if (!recent.length) {
      els.sparklineBox.appendChild(el("span", "stat-note", "No completed runs yet"));
      els.statSparkNote.textContent = "Latest completed runs (oldest to newest)";
      return;
    }
    const width = 300;
    const height = 34;
    const pad = 3;
    const maxV = Math.max(1, ...recent);
    const minV = Math.min(0, ...recent);
    const span = Math.max(1, maxV - minV);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("class", "sparkline-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Recent wall times sparkline");

    const coords = recent.map((v, idx) => {
      const x = recent.length === 1
        ? width / 2
        : pad + (idx / (recent.length - 1)) * (width - 2 * pad);
      const y = height - pad - ((v - minV) / span) * (height - 2 * pad);
      return [x.toFixed(1), y.toFixed(1)];
    });

    if (coords.length > 1) {
      const poly = document.createElementNS(SVG_NS, "polyline");
      poly.setAttribute("class", "sparkline-line");
      poly.setAttribute("points", coords.map((p) => p[0] + "," + p[1]).join(" "));
      svg.appendChild(poly);
    }
    const last = coords[coords.length - 1];
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("class", "sparkline-dot");
    dot.setAttribute("cx", last[0]);
    dot.setAttribute("cy", last[1]);
    dot.setAttribute("r", "2.6");
    svg.appendChild(dot);

    els.sparklineBox.appendChild(svg);
    els.statSparkNote.textContent = "Latest " + recent.length + " run(s), max " + Math.round(maxV) + " ms";
  }

  function renderSummary(summary) {
    clearNode(els.summaryPills);
    const defs = [
      ["native", "native", null],
      ["container", "container", null],
      ["degraded", "degraded", "degraded"],
      ["none", "none", null],
      ["blocked", "blocked", "blocked"],
      ["timed_out", "timed out", "timed_out"],
      ["no_end", "no end", "no_end"],
      ["denied", "denied syscalls", "denied"],
    ];
    for (const [key, label, filterValue] of defs) {
      const btn = el("button", "pill" + (filterValue && state.filter === filterValue ? " is-selected" : ""));
      btn.type = "button";
      btn.appendChild(el("span", null, label));
      btn.appendChild(el("span", "pill-count", summary[key] || 0));
      if (filterValue) {
        btn.addEventListener("click", () => {
          state.filter = state.filter === filterValue ? "all" : filterValue;
          els.filterSelect.value = state.filter;
          refreshRuns();
        });
      } else {
        btn.addEventListener("click", () => {
          state.filter = "all";
          els.filterSelect.value = "all";
          refreshRuns();
        });
      }
      els.summaryPills.appendChild(btn);
    }
  }

  function renderStats(calls) {
    const walls = [];
    const mems = [];
    let containerCount = 0;
    for (const c of calls) {
      if (typeof c.wall_ms === "number" && Number.isFinite(c.wall_ms)) walls.push(c.wall_ms);
      if (c.usage && typeof c.usage.mem_peak_mb === "number" && Number.isFinite(c.usage.mem_peak_mb)) {
        mems.push(c.usage.mem_peak_mb);
      }
      if (c.isolation === "container" || c.isolation === "container-degraded") {
        containerCount += 1;
      }
    }
    const medWall = median(walls);
    els.statWall.textContent = medWall === null ? "—" : Math.round(medWall) + " ms";
    const medMem = median(mems);
    if (medMem === null) {
      els.statMem.textContent = containerCount > 0 ? "not measured" : "—";
      els.statMemNote.textContent =
        "Not measured on container runs; on native runs, " + WHOLE_SANDBOX_NOTE;
    } else {
      els.statMem.textContent = medMem.toFixed(1).replace(/\.0$/, "") + " MiB";
      els.statMemNote.textContent =
        "Native runs only (" + WHOLE_SANDBOX_NOTE + "); not measured on container runs";
    }
    renderSparkline(calls);
  }

  function addKv(grid, key, value) {
    if (value === undefined || value === null || value === "") return;
    grid.appendChild(el("div", "kv-key", key));
    grid.appendChild(el("div", "kv-val", value));
  }

  function renderDetail(call) {
    clearNode(els.runDetail);
    if (!call) {
      els.runDetail.appendChild(
        el("div", "empty-state", "Select a call to inspect its full logged code, isolation, and usage.")
      );
      return;
    }

    const head = el("div", "detail-head");
    head.appendChild(el("span", "detail-id", call.id));
    if (call.language) head.appendChild(el("span", "badge badge-muted", call.language));
    head.appendChild(outcomeBadge(call));
    if (call.isolation) {
      head.appendChild(el("span", isolationBadgeClass(call.isolation), "isolation: " + call.isolation));
    }
    els.runDetail.appendChild(head);

    if (call.has_start && !call.has_end) {
      const note = el("div", "note-box", "No end record (" + NO_END_TOOLTIP + ").");
      els.runDetail.appendChild(note);
    } else if (!call.has_start && call.has_end) {
      els.runDetail.appendChild(el("div", "note-box", "(no start record found)"));
    }

    if (call.reason) {
      const sec = el("div", "detail-section");
      sec.appendChild(el("h3", "detail-section-title", "Reason"));
      sec.appendChild(el("pre", "code-block", call.reason));
      els.runDetail.appendChild(sec);
    }

    if (call.has_start) {
      const sec = el("div", "detail-section");
      const title = call.code_truncated ? "Code (truncated at 4 KiB audit cap)" : "Code";
      sec.appendChild(el("h3", "detail-section-title", title));
      const pre = el("pre", "code-block");
      pre.appendChild(el("code", null, call.code || ""));
      sec.appendChild(pre);
      els.runDetail.appendChild(sec);
    }

    const isoSec = el("div", "detail-section");
    isoSec.appendChild(el("h3", "detail-section-title", "Isolation & Execution"));
    const isoGrid = el("div", "kv-grid");
    addKv(isoGrid, "timestamp", call.ts);
    addKv(isoGrid, "end_ts", call.end_ts);
    addKv(isoGrid, "isolation", call.isolation);
    addKv(isoGrid, "isolation_detail", call.isolation_detail);
    addKv(isoGrid, "exit_code", call.exit_code !== null ? String(call.exit_code) : null);
    addKv(isoGrid, "backend", call.backend);
    addKv(isoGrid, "runtime", call.runtime);
    addKv(isoGrid, "verdict", call.verdict);
    addKv(isoGrid, "image_ran", call.image_ran || call.image);
    addKv(isoGrid, "backend_run", call.backend_run);
    addKv(isoGrid, "timeout_s", call.timeout_s !== null ? call.timeout_s + " s" : null);
    if (call.client) {
      addKv(isoGrid, "client", [call.client.name, call.client.version].filter(Boolean).join(" "));
    }
    addKv(isoGrid, "code_sha256", call.code_sha256);
    addKv(isoGrid, "audit_file", call.file);
    isoSec.appendChild(isoGrid);
    els.runDetail.appendChild(isoSec);

    const useSec = el("div", "detail-section");
    useSec.appendChild(el("h3", "detail-section-title", "Usage & Output"));
    const useGrid = el("div", "kv-grid");
    addKv(useGrid, "wall_ms", call.wall_ms !== null ? call.wall_ms + " ms" : null);
    if (call.usage) {
      addKv(useGrid, "cpu_ms", call.usage.cpu_ms !== null ? call.usage.cpu_ms + " ms" : null);
      addKv(useGrid, "mem_peak_mb", call.usage.mem_peak_mb !== null ? call.usage.mem_peak_mb + " MiB" : null);
      addKv(useGrid, "pids_peak", call.usage.pids_peak !== null ? String(call.usage.pids_peak) : null);
    }
    addKv(useGrid, "not_measured", call.not_measured);
    if (call.stdout_bytes !== null) addKv(useGrid, "stdout_bytes", String(call.stdout_bytes));
    if (call.stderr_bytes !== null) addKv(useGrid, "stderr_bytes", String(call.stderr_bytes));
    if (call.output_capped) addKv(useGrid, "output_capped", "true");
    if (call.limit_hits && Object.keys(call.limit_hits).length) {
      addKv(
        useGrid,
        "limit_hits",
        Object.entries(call.limit_hits).map(([k, v]) => k + "=" + v).join(", ")
      );
    }
    if (call.denied_syscalls && call.denied_syscalls.length) {
      addKv(useGrid, "denied_syscalls", call.denied_syscalls.join(", "));
    }
    useSec.appendChild(useGrid);
    if (call.usage && (call.usage.cpu_ms !== null || call.usage.mem_peak_mb !== null || call.usage.pids_peak !== null)) {
      useSec.appendChild(el("div", "note-box", "Note: kernel usage " + WHOLE_SANDBOX_NOTE + "."));
    }
    els.runDetail.appendChild(useSec);
  }

  function updateDetail(call) {
    const nextJson = call ? JSON.stringify(call) : "";
    if (nextJson === state.lastDetailJson) return;
    state.lastDetailJson = nextJson;
    renderDetail(call);
  }

  function renderRuns(data) {
    state.runsData = data;
    renderSummary(data.summary);
    renderStats(data.calls);

    let scope =
      "Showing " + data.returned_calls + " of " + data.matched_calls +
      " matching (" + data.total_calls + " total)";
    if (data.skipped_lines) scope += " | " + data.skipped_lines + " skipped";
    if (data.failed_files) scope += " | " + data.failed_files + " failed file(s)";
    els.scopeNote.textContent = scope;

    if (state.meta) {
      const dirStatus = data.audit_dir_exists ? data.file_count + " file(s)" : "directory does not exist yet";
      els.runsMetaNote.textContent = "Audit dir: " + state.meta.audit_dir + " (" + dirStatus + ")";
    }

    clearNode(els.runsList);
    if (!data.audit_dir_exists) {
      els.runsList.appendChild(
        el("div", "empty-state", "Audit directory does not exist yet (not created). Run a tool call via MCP to see records appear live.")
      );
      updateDetail(null);
      return;
    }
    if (!data.calls.length) {
      const msg = data.total_calls === 0
        ? "No audit records yet. Calls made via vestibule-mcp will appear here within 1 second."
        : "No calls match the current filter or search.";
      els.runsList.appendChild(el("div", "empty-state", msg));
      updateDetail(null);
      return;
    }

    let selectedCall = null;
    if (state.selectedKey !== null) {
      for (const call of data.calls) {
        if (callKey(call) === state.selectedKey) {
          selectedCall = call;
          break;
        }
      }
    }
    if (!selectedCall) {
      selectedCall = data.calls[0];
      state.selectedKey = callKey(selectedCall);
    }

    for (const call of data.calls) {
      const key = callKey(call);
      const isSelected = key === state.selectedKey;

      const row = el("button", "run-row" + (isSelected ? " is-selected" : ""));
      row.type = "button";
      row.appendChild(el("span", "run-ts", formatShortTs(call.ts)));
      row.appendChild(el("span", "badge badge-muted", call.language || "—"));
      row.appendChild(outcomeBadge(call));
      row.appendChild(el("span", "run-snippet", firstCodeLine(call)));
      const right = el("span", "run-wall");
      if (call.isolation) {
        right.appendChild(el("span", isolationBadgeClass(call.isolation), call.isolation));
      } else if (call.wall_ms !== null) {
        right.textContent = Math.round(call.wall_ms) + "ms";
      }
      row.appendChild(right);

      row.addEventListener("click", () => {
        state.selectedKey = key;
        renderRuns(state.runsData);
      });
      els.runsList.appendChild(row);
    }

    updateDetail(selectedCall);
  }

  function renderBreadcrumbs(rawPath, displayPath) {
    clearNode(els.wsBreadcrumbs);
    const rootBtn = el("button", "crumb", "/workspace");
    rootBtn.type = "button";
    rootBtn.addEventListener("click", () => loadWorkspace("."));
    els.wsBreadcrumbs.appendChild(rootBtn);

    if (!rawPath || rawPath === ".") return;
    const rawParts = rawPath.replace(/\\/g, "/").split("/").filter((s) => s && s !== ".");
    const dispParts = (displayPath || rawPath).split("/").filter((s) => s && s !== ".");
    let accum = "";
    for (let i = 0; i < rawParts.length; i++) {
      accum = accum ? accum + "/" + rawParts[i] : rawParts[i];
      const target = accum;
      els.wsBreadcrumbs.appendChild(el("span", "crumb-sep", "/"));
      const btn = el("button", "crumb", dispParts[i] !== undefined ? dispParts[i] : rawParts[i]);
      btn.type = "button";
      btn.addEventListener("click", () => loadWorkspace(target));
      els.wsBreadcrumbs.appendChild(btn);
    }
  }

  function renderWorkspace(data) {
    renderBreadcrumbs(data.path, data.display_path);
    els.wsBanner.className = "banner is-hidden";
    els.wsBanner.textContent = "";
    clearNode(els.wsBody);

    if (state.meta) {
      const wsDir = state.meta.workspace_dir || "(unavailable)";
      els.wsMetaNote.textContent = "Workspace: " + wsDir + " (preview cap " + state.meta.max_preview_bytes + " B)";
    }

    if (data.kind === "refused") {
      els.wsBanner.className = "banner banner-danger";
      els.wsBanner.textContent = "Refused by path jail (" + data.display_path + "): " + data.reason;
      els.wsBody.appendChild(
        el("div", "empty-state", "Symlinks, reparse points, and paths outside /workspace are never followed.")
      );
      return;
    }

    if (data.kind === "error") {
      els.wsBanner.className = "banner banner-warn";
      els.wsBanner.textContent = "Workspace error: " + data.reason;
      els.wsBody.appendChild(el("div", "empty-state", "Could not inspect workspace path."));
      return;
    }

    if (data.kind === "missing") {
      const msg = data.root_exists
        ? "Not found in workspace: " + data.display_path
        : "Workspace directory does not exist yet (not created). It will appear once a run writes to /workspace.";
      els.wsBody.appendChild(el("div", "empty-state", msg));
      return;
    }

    if (data.kind === "file") {
      const wrap = el("div", "ws-file-wrap");
      if (data.truncated) {
        const warn = el("div", "note-box", "Truncated at " + data.max_bytes + " bytes.");
        wrap.appendChild(warn);
      }
      const pre = el("pre", "code-block");
      pre.textContent = data.content;
      wrap.appendChild(pre);
      els.wsBody.appendChild(wrap);
      return;
    }

    if (data.kind === "dir") {
      if (!data.entries.length) {
        els.wsBody.appendChild(el("div", "empty-state", "(empty directory)"));
      } else {
        const table = el("table", "ws-table");
        const thead = document.createElement("thead");
        const htr = document.createElement("tr");
        htr.appendChild(el("th", null, "Name"));
        htr.appendChild(el("th", null, "Kind"));
        htr.appendChild(el("th", null, "Size"));
        thead.appendChild(htr);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        const base = data.path && data.path !== "." ? data.path.replace(/\/+$/, "") : "";
        for (const entry of data.entries) {
          const tr = document.createElement("tr");
          const tdName = document.createElement("td");
          const btn = el(
            "button",
            "ws-link" + (entry.kind === "symlink" ? " ws-link-symlink" : ""),
            entry.display_name
          );
          btn.type = "button";
          const nextPath = base ? base + "/" + entry.name : entry.name;
          btn.addEventListener("click", () => loadWorkspace(nextPath));
          tdName.appendChild(btn);
          tr.appendChild(tdName);

          const tdKind = document.createElement("td");
          const kCls = entry.kind === "symlink"
            ? "badge badge-warn"
            : entry.kind === "dir"
              ? "badge badge-info"
              : "badge badge-muted";
          tdKind.appendChild(el("span", kCls, entry.kind));
          tr.appendChild(tdKind);

          const sizeText = entry.size === null || entry.size === undefined ? "—" : entry.size + " B";
          tr.appendChild(el("td", null, sizeText));
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        els.wsBody.appendChild(table);
      }

      if (data.hidden > 0 || data.over_scan) {
        const suffix = data.over_scan ? "+ more entries not shown (scan cap reached)" : " more entries not shown";
        els.wsBody.appendChild(el("div", "ws-footer", data.hidden + suffix));
      }
    }
  }

  async function refreshMeta(gen) {
    try {
      const meta = await apiGet("/api/meta", null, gen);
      if (!meta || gen !== state.tokenGen || !state.token) return false;
      state.meta = meta;
      els.versionBadge.textContent = "v" + meta.version;
      if (meta.audit_warning) {
        els.warningBanner.textContent = "Audit dir warning: " + meta.audit_warning;
        els.warningBanner.classList.remove("is-hidden");
      } else {
        els.warningBanner.classList.add("is-hidden");
      }
      return true;
    } catch (_err) {
      if (gen === state.tokenGen && state.token) {
        setStatus("error", "Disconnected");
      }
      return false;
    }
  }

  async function refreshRuns() {
    const seq = ++state.runsSeq;
    const gen = state.tokenGen;
    try {
      const data = await apiGet("/api/runs", {
        limit: state.limit,
        filter: state.filter,
        q: state.q,
      }, gen);
      if (!data || seq !== state.runsSeq || gen !== state.tokenGen || !state.token) return;
      els.tokenBanner.classList.add("is-hidden");
      if (!state.meta) refreshMeta(gen);
      renderRuns(data);
      setStatus(state.paused ? "paused" : "live", state.paused ? "Paused" : "Live (1s)");
    } catch (_err) {
      if (seq === state.runsSeq && gen === state.tokenGen && state.token) {
        setStatus("error", "Disconnected");
      }
    }
  }

  async function loadWorkspace(path) {
    state.wsPath = path || ".";
    const seq = ++state.wsSeq;
    const gen = state.tokenGen;
    try {
      const data = await apiGet("/api/workspace", { path: state.wsPath }, gen);
      if (!data || seq !== state.wsSeq || gen !== state.tokenGen || !state.token) return;
      els.tokenBanner.classList.add("is-hidden");
      if (!state.meta) refreshMeta(gen);
      renderWorkspace(data);
      setStatus(state.paused ? "paused" : "live", state.paused ? "Paused" : "Live (1s)");
    } catch (_err) {
      if (seq === state.wsSeq && gen === state.tokenGen && state.token) {
        setStatus("error", "Disconnected");
      }
    }
  }

  function startPolling() {
    stopPolling();
    if (state.paused || !state.token) return;
    state.pollTimer = window.setInterval(() => {
      if (state.paused || !state.token) return;
      if (state.view === "runs") refreshRuns();
    }, 1000);
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function switchView(view) {
    state.view = view;
    els.tabRuns.classList.toggle("is-active", view === "runs");
    els.tabWorkspace.classList.toggle("is-active", view === "workspace");
    els.viewRuns.classList.toggle("is-hidden", view !== "runs");
    els.viewWorkspace.classList.toggle("is-hidden", view !== "workspace");
    if (view === "workspace") {
      loadWorkspace(state.wsPath);
    } else {
      refreshRuns();
    }
  }

  async function connect() {
    const gen = state.tokenGen;
    try {
      const ok = await refreshMeta(gen);
      if (!ok || gen !== state.tokenGen || !state.token) {
        if (gen === state.tokenGen && state.token) startPolling();
        return;
      }
      els.tokenBanner.classList.add("is-hidden");
      await refreshRuns();
      if (gen !== state.tokenGen || !state.token) return;
      if (state.view === "workspace") await loadWorkspace(state.wsPath);
      if (gen !== state.tokenGen || !state.token) return;
      startPolling();
    } catch (_err) {
      if (gen === state.tokenGen && state.token) {
        setStatus("error", "Disconnected");
        startPolling();
      }
    }
  }

  els.tabRuns.addEventListener("click", () => switchView("runs"));
  els.tabWorkspace.addEventListener("click", () => switchView("workspace"));

  els.togglePoll.addEventListener("click", () => {
    state.paused = !state.paused;
    els.togglePoll.textContent = state.paused ? "Resume" : "Pause";
    if (state.paused) {
      stopPolling();
      setStatus("paused", "Paused");
    } else {
      refreshRuns();
      startPolling();
    }
  });

  els.filterSelect.addEventListener("change", () => {
    state.filter = els.filterSelect.value;
    refreshRuns();
  });

  els.limitSelect.addEventListener("change", () => {
    state.limit = els.limitSelect.value;
    refreshRuns();
  });

  let searchTimer = null;
  els.searchInput.addEventListener("input", () => {
    state.q = els.searchInput.value;
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => refreshRuns(), 180);
  });

  els.wsRefresh.addEventListener("click", () => loadWorkspace(state.wsPath));

  els.tokenForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const val = els.tokenInput.value.trim();
    if (!val) return;
    if (!TOKEN_RE.test(val)) {
      els.tokenMessage.textContent = "Invalid token: must contain only printable ASCII characters.";
      els.tokenBanner.classList.remove("is-hidden");
      return;
    }
    state.tokenGen += 1;
    state.token = val;
    state.meta = null;
    try {
      window.sessionStorage.setItem(TOKEN_KEY, val);
    } catch (_err) {
      // ignore
    }
    els.tokenInput.value = "";
    state.paused = false;
    els.togglePoll.textContent = "Pause";
    setStatus("connecting", "Connecting");
    connect();
  });

  initToken();
  if (state.token) {
    connect();
  } else {
    requireToken("Session token required. Paste the token from the URL printed by vestibule-mcp ui.");
  }
})();
