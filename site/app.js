(() => {
  "use strict";

  const POS_INT_RE = /^[1-9][0-9]{0,5}$/;
  const COPY_RESET_MS = 1800;

  function validateNumericField(raw, fieldName, defaults) {
    const trimmed = String(raw == null ? "" : raw).trim();
    if (trimmed === "") {
      return { valid: true, value: null, isDefault: true, error: null };
    }
    if (!POS_INT_RE.test(trimmed)) {
      const msg = fieldName === "default_timeout_s"
        ? "Enter a whole number from 1 to " + defaults.max_timeout_s + "."
        : "Enter a positive whole number (1–999999).";
      return { valid: false, value: null, isDefault: false, error: msg };
    }
    const num = Number(trimmed);
    if (fieldName === "default_timeout_s" && num > defaults.max_timeout_s) {
      return {
        valid: false,
        value: null,
        isDefault: false,
        error: "Must be 1–" + defaults.max_timeout_s + " (clamped by VESTIBULE_MAX_TIMEOUT_S).",
      };
    }
    return {
      valid: true,
      value: num,
      isDefault: num === defaults[fieldName],
      error: null,
    };
  }

  function collectOverrides(options, defaults) {
    const memCheck = validateNumericField(options.memMb, "mem_mb", defaults);
    const cpuCheck = validateNumericField(options.cpuPct, "cpu_pct", defaults);
    const timeoutCheck = validateNumericField(
      options.defaultTimeoutS,
      "default_timeout_s",
      defaults
    );

    const env = {};
    if (options.workspaceRo) {
      env.VESTIBULE_WORKSPACE_RO = "1";
    }
    if (memCheck.valid && !memCheck.isDefault && memCheck.value !== null) {
      env.VESTIBULE_MEM_MB = String(memCheck.value);
    }
    if (cpuCheck.valid && !cpuCheck.isDefault && cpuCheck.value !== null) {
      env.VESTIBULE_CPU_PCT = String(cpuCheck.value);
    }
    if (timeoutCheck.valid && !timeoutCheck.isDefault && timeoutCheck.value !== null) {
      env.VESTIBULE_DEFAULT_TIMEOUT_S = String(timeoutCheck.value);
    }

    return {
      env,
      errors: {
        memMb: memCheck.error,
        cpuPct: cpuCheck.error,
        defaultTimeoutS: timeoutCheck.error,
      },
    };
  }

  function buildClaudeAddCommand(overrides) {
    const parts = ["claude", "mcp", "add", "--scope", "user", "vestibule"];
    const keys = Object.keys(overrides);
    for (let i = 0; i < keys.length; i += 1) {
      parts.push("-e", keys[i] + "=" + overrides[keys[i]]);
    }
    parts.push("--", "vestibule-mcp");
    return parts.join(" ");
  }

  function buildMcpServerJson(platformKey, clientKey, overrides, data) {
    const isMacGui = platformKey === "macos" && (clientKey === "cursor" || clientKey === "claude-desktop");
    const command = isMacGui ? data.platforms.macos.macos_bin_path : "vestibule-mcp";

    const envObj = {};
    if (isMacGui) {
      envObj.PATH = data.platforms.macos.macos_path_env;
    }
    const keys = Object.keys(overrides);
    for (let i = 0; i < keys.length; i += 1) {
      envObj[keys[i]] = overrides[keys[i]];
    }

    const entry = { command };
    if (Object.keys(envObj).length > 0) {
      entry.env = envObj;
    }
    return JSON.stringify({ mcpServers: { vestibule: entry } }, null, 2);
  }

  function buildWizardPlan(state, data) {
    const platformKey = Object.prototype.hasOwnProperty.call(data.platforms, state.platform)
      ? state.platform
      : "linux";
    const clientKey = Object.prototype.hasOwnProperty.call(data.clients, state.client)
      ? state.client
      : "claude-code";

    const plat = data.platforms[platformKey];
    const client = data.clients[clientKey];
    const overrideResult = collectOverrides(state, data.defaults);
    const overrides = overrideResult.env;

    const installLines = [
      "uv tool install vestibule-mcp",
      "uv tool update-shell" + (plat.update_shell_comment || ""),
      "vestibule-mcp setup # downloads ~123 MB of pinned runtimes and checks isolation",
    ];
    if (clientKey === "claude-code") {
      installLines.push(buildClaudeAddCommand(overrides));
    }

    const macBinHint = data.platforms.macos.macos_bin_hint;
    let installNote = null;
    if (platformKey === "macos" && clientKey === "claude-code") {
      installNote =
        "On macOS, if Claude Code cannot find vestibule-mcp on PATH, replace vestibule-mcp with the full binary path from `" + macBinHint + "`.";
    }

    let configSection = null;
    let lockdownSection = null;

    if (clientKey === "claude-code") {
      const prefix = plat.is_windows ? data.lockdown.windows_prefix : "";
      lockdownSection = {
        active: Boolean(state.lockdown),
        baseCmd: prefix + data.lockdown.base_args,
        strictCmd: prefix + data.lockdown.strict_args,
        onlyVestibuleJson: buildMcpServerJson(platformKey, clientKey, overrides, data),
        caveat: data.lockdown.caveat,
      };
    } else if (clientKey === "cursor") {
      const macHint = platformKey === "macos"
        ? " Replace " + data.platforms.macos.macos_bin_path + " with the exact path from `" + macBinHint + "`."
        : "";
      const testNote = platformKey === "windows"
        ? "Merge into mcpServers if the file already exists, then fully restart Cursor (tested on Windows)."
        : "Merge into mcpServers if the file already exists, then fully restart Cursor. Note: Cursor integration has been tested on Windows only." + macHint;
      configSection = {
        title: "4. Add Vestibule to Cursor",
        pathLabel: client.config_path,
        pathStatus: "tested on Windows",
        note: testNote,
        isWarning: platformKey === "macos",
        jsonText: buildMcpServerJson(platformKey, clientKey, overrides, data),
      };
    } else if (clientKey === "claude-desktop") {
      if (plat.claude_desktop_path) {
        const macHint = platformKey === "macos"
          ? " Replace " + data.platforms.macos.macos_bin_path + " with the exact path from `" + macBinHint + "` (untested on macOS)."
          : " Merge into mcpServers if it already exists, then fully restart Claude Desktop.";
        configSection = {
          title: "4. Add Vestibule to Claude Desktop",
          pathLabel: plat.claude_desktop_path,
          pathStatus: plat.claude_desktop_status,
          note: "Config path (" + plat.claude_desktop_status + ")." + macHint,
          isWarning: platformKey === "macos",
          jsonText: buildMcpServerJson(platformKey, clientKey, overrides, data),
        };
      } else {
        configSection = {
          title: "4. Claude Desktop on " + plat.label,
          pathLabel: "Generic mcpServers JSON (no official Linux Claude Desktop path)",
          pathStatus: null,
          note: plat.claude_desktop_note,
          isWarning: true,
          jsonText: buildMcpServerJson(platformKey, clientKey, overrides, data),
        };
      }
    } else {
      const macNote = platformKey === "macos"
        ? "On macOS, GUI clients that do not inherit your shell PATH may need the full binary path from `" + macBinHint + "` and an explicit PATH entry (such as " + data.platforms.macos.macos_path_env + ")."
        : "Merge this entry into your MCP client's mcpServers configuration and restart the client.";
      configSection = {
        title: "4. Add Vestibule to your MCP client",
        pathLabel: "mcpServers JSON",
        pathStatus: null,
        note: macNote,
        isWarning: false,
        jsonText: buildMcpServerJson(platformKey, clientKey, overrides, data),
      };
    }

    return {
      platform: platformKey,
      client: clientKey,
      isolation: plat.isolation,
      badge: plat.badge,
      platformNote: plat.note,
      prereqTitle: plat.prereq_title,
      prereqItems: plat.prereq_items,
      uvLabel: "Install uv, then open a new terminal",
      uvCmd: plat.uv_cmd,
      installCmd: installLines.join("\n"),
      installNote,
      configSection,
      lockdownSection,
      verifyCmd: data.verify.audit_cmd + "\n" + data.verify.ui_cmd,
      overrides,
      errors: overrideResult.errors,
    };
  }

  // Export pure builders for Node dev/build verification without touching DOM.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      validateNumericField,
      collectOverrides,
      buildClaudeAddCommand,
      buildMcpServerJson,
      buildWizardPlan,
    };
  }

  if (typeof document === "undefined") {
    return;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearNode(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  async function copyText(text) {
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_err) {
        // Fall through to off-screen textarea fallback (e.g., file:// contexts).
      }
    }
    const prevFocus = document.activeElement;
    const ta = document.createElement("textarea");
    ta.className = "copy-scratch";
    ta.setAttribute("readonly", "");
    ta.setAttribute("aria-hidden", "true");
    ta.tabIndex = -1;
    ta.value = text;
    document.body.appendChild(ta);
    let ok = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = Boolean(document.execCommand("copy"));
    } catch (_err) {
      ok = false;
    } finally {
      if (ta.parentNode) {
        ta.parentNode.removeChild(ta);
      }
      if (prevFocus && typeof prevFocus.focus === "function") {
        prevFocus.focus({ preventScroll: true });
      }
    }
    return ok;
  }

  const copyTimers = new WeakMap();

  function attachCopyButton(btn, getText) {
    btn.addEventListener("click", async () => {
      const prevTimer = copyTimers.get(btn);
      if (prevTimer) {
        window.clearTimeout(prevTimer);
      }
      const ok = await copyText(getText());
      btn.textContent = ok ? "Copied" : "Copy failed";
      btn.classList.remove("is-copied", "is-failed");
      btn.classList.add(ok ? "is-copied" : "is-failed");
      const timer = window.setTimeout(() => {
        btn.textContent = "Copy";
        btn.classList.remove("is-copied", "is-failed");
      }, COPY_RESET_MS);
      copyTimers.set(btn, timer);
    });
  }

  function createCodePanel(label, codeText) {
    const panel = el("div", "code-panel");
    const bar = el("div", "code-panel-bar");
    bar.appendChild(el("span", "code-panel-label", label));
    const btn = el("button", "btn btn-copy", "Copy");
    btn.type = "button";
    attachCopyButton(btn, () => codeText);
    bar.appendChild(btn);
    panel.appendChild(bar);

    const pre = el("pre", "code-block");
    const code = el("code", null, codeText);
    pre.appendChild(code);
    panel.appendChild(pre);
    return panel;
  }

  function statusClassForPlatform(platformKey, isolation) {
    if (platformKey === "macos") return "step-status step-status-warn";
    if (isolation === "native") return "step-status step-status-native";
    return "step-status";
  }

  function initHeroCopy() {
    const buttons = document.querySelectorAll("[data-copy-target]");
    for (let i = 0; i < buttons.length; i += 1) {
      const btn = buttons[i];
      const targetId = btn.getAttribute("data-copy-target");
      const target = targetId ? document.getElementById(targetId) : null;
      if (target) {
        attachCopyButton(btn, () => target.textContent || "");
      }
    }
  }

  function initChamberTabs() {
    const tablist = document.querySelector('[role="tablist"]');
    if (!tablist) return;
    const tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;

    function activateTab(nextTab, moveFocus) {
      for (let i = 0; i < tabs.length; i += 1) {
        const tab = tabs[i];
        const selected = tab === nextTab;
        tab.setAttribute("aria-selected", selected ? "true" : "false");
        tab.tabIndex = selected ? 0 : -1;
        tab.classList.toggle("is-active", selected);
        const panelId = tab.getAttribute("aria-controls");
        const panel = panelId ? document.getElementById(panelId) : null;
        if (panel) {
          panel.hidden = !selected;
        }
      }
      if (moveFocus) {
        nextTab.focus();
      }
    }

    for (let i = 0; i < tabs.length; i += 1) {
      const idx = i;
      tabs[i].addEventListener("click", () => {
        activateTab(tabs[idx], false);
      });
      tabs[i].addEventListener("keydown", (event) => {
        let nextIndex = null;
        if (event.key === "ArrowRight") {
          nextIndex = (idx + 1) % tabs.length;
        } else if (event.key === "ArrowLeft") {
          nextIndex = (idx - 1 + tabs.length) % tabs.length;
        } else if (event.key === "Home") {
          nextIndex = 0;
        } else if (event.key === "End") {
          nextIndex = tabs.length - 1;
        }
        if (nextIndex !== null) {
          event.preventDefault();
          activateTab(tabs[nextIndex], true);
        }
      });
    }
  }

  function initWizard() {
    const dataEl = document.getElementById("vestibule-data");
    const form = document.getElementById("wizard-form");
    const outputEl = document.getElementById("wizard-output");
    if (!dataEl || !form || !outputEl) return;

    const data = JSON.parse(dataEl.textContent || "{}");
    const platformSummaryEl = document.getElementById("platform-summary");
    const workspaceRoEl = document.getElementById("opt-workspace-ro");
    const lockdownWrapEl = document.getElementById("opt-lockdown-wrap");
    const lockdownEl = document.getElementById("opt-lockdown");
    const memEl = document.getElementById("opt-mem-mb");
    const cpuEl = document.getElementById("opt-cpu-pct");
    const timeoutEl = document.getElementById("opt-timeout-s");
    const errMemEl = document.getElementById("err-mem-mb");
    const errCpuEl = document.getElementById("err-cpu-pct");
    const errTimeoutEl = document.getElementById("err-timeout-s");

    function readRadio(name, fallback) {
      const checked = form.querySelector('input[name="' + name + '"]:checked');
      return checked ? checked.value : fallback;
    }

    function setFieldError(inputEl, errEl, message) {
      if (!inputEl || !errEl) return;
      if (message) {
        errEl.textContent = message;
        errEl.classList.remove("is-hidden");
        inputEl.classList.add("is-invalid");
        inputEl.setAttribute("aria-invalid", "true");
      } else {
        errEl.textContent = "";
        errEl.classList.add("is-hidden");
        inputEl.classList.remove("is-invalid");
        inputEl.removeAttribute("aria-invalid");
      }
    }

    let lastPlanJson = null;

    function render() {
      const client = readRadio("client", "claude-code");
      const isClaudeCode = client === "claude-code";
      if (lockdownWrapEl) {
        lockdownWrapEl.classList.toggle("is-hidden", !isClaudeCode);
      }

      const state = {
        platform: readRadio("platform", "linux"),
        client,
        workspaceRo: Boolean(workspaceRoEl && workspaceRoEl.checked),
        lockdown: Boolean(isClaudeCode && lockdownEl && lockdownEl.checked),
        memMb: memEl ? memEl.value : String(data.defaults.mem_mb),
        cpuPct: cpuEl ? cpuEl.value : String(data.defaults.cpu_pct),
        defaultTimeoutS: timeoutEl ? timeoutEl.value : String(data.defaults.default_timeout_s),
      };

      const plan = buildWizardPlan(state, data);

      setFieldError(memEl, errMemEl, plan.errors.memMb);
      setFieldError(cpuEl, errCpuEl, plan.errors.cpuPct);
      setFieldError(timeoutEl, errTimeoutEl, plan.errors.defaultTimeoutS);

      const { errors: _ignoredErrors, ...renderablePlan } = plan;
      const planJson = JSON.stringify(renderablePlan);
      if (planJson === lastPlanJson) {
        return;
      }
      lastPlanJson = planJson;

      if (platformSummaryEl) {
        clearNode(platformSummaryEl);
        platformSummaryEl.appendChild(el("span", "choice-meta-iso", plan.badge));
        platformSummaryEl.appendChild(document.createTextNode(" · " + plan.platformNote));
      }

      clearNode(outputEl);

      // Step 1: Prerequisites & platform caveats
      const prereqCard = el("div", "step-block");
      const prereqHead = el("div", "step-header");
      prereqHead.appendChild(el("h3", "step-title", "1. Prerequisites — " + plan.prereqTitle));
      prereqHead.appendChild(
        el("span", statusClassForPlatform(plan.platform, plan.isolation), plan.badge)
      );
      prereqCard.appendChild(prereqHead);
      const prereqList = el("ul", "step-list");
      for (let i = 0; i < plan.prereqItems.length; i += 1) {
        prereqList.appendChild(el("li", null, plan.prereqItems[i]));
      }
      prereqCard.appendChild(prereqList);
      outputEl.appendChild(prereqCard);

      // Step 2: Install uv
      const uvCard = el("div", "step-block");
      const uvHead = el("div", "step-header");
      uvHead.appendChild(el("h3", "step-title", "2. " + plan.uvLabel));
      uvCard.appendChild(uvHead);
      const shellLabel = plan.platform === "windows" ? "PowerShell" : "Shell";
      uvCard.appendChild(createCodePanel(shellLabel, plan.uvCmd));
      outputEl.appendChild(uvCard);

      // Step 3: Install Vestibule, run setup (+ connect Claude Code)
      const installCard = el("div", "step-block");
      const installHead = el("div", "step-header");
      const installTitle = plan.client === "claude-code"
        ? "3. Install Vestibule, run setup, and register with Claude Code"
        : "3. Install Vestibule and run setup";
      installHead.appendChild(el("h3", "step-title", installTitle));
      installCard.appendChild(installHead);
      installCard.appendChild(createCodePanel(shellLabel, plan.installCmd));
      if (plan.installNote) {
        installCard.appendChild(el("p", "step-note", plan.installNote));
      }
      outputEl.appendChild(installCard);

      // Step 4: Client config or Claude Code lockdown
      if (plan.configSection) {
        const cfg = plan.configSection;
        const cfgCard = el("div", "step-block");
        const cfgHead = el("div", "step-header");
        cfgHead.appendChild(el("h3", "step-title", cfg.title));
        if (cfg.pathStatus) {
          cfgHead.appendChild(el("span", "step-status", cfg.pathStatus));
        }
        cfgCard.appendChild(cfgHead);
        cfgCard.appendChild(
          el("p", cfg.isWarning ? "step-note-warn" : "step-note", cfg.note)
        );
        cfgCard.appendChild(createCodePanel(cfg.pathLabel, cfg.jsonText));
        outputEl.appendChild(cfgCard);
      } else if (plan.lockdownSection) {
        const ld = plan.lockdownSection;
        const ldCard = el("div", "step-block");
        const ldHead = el("div", "step-header");
        ldHead.appendChild(
          el(
            "h3",
            "step-title",
            ld.active ? "4. Sandbox-only Claude Code lockdown" : "4. Optional: lock down Claude Code"
          )
        );
        ldCard.appendChild(ldHead);
        if (!ld.active) {
          ldCard.appendChild(
            el(
              "p",
              "step-note",
              "Vestibule is registered above. Remember: only code sent to run_code is sandboxed — check \"Sandbox-only lockdown\" above to disable Claude Code's built-in shell and file tools."
            )
          );
        } else {
          ldCard.appendChild(
            el(
              "p",
              "step-note",
              "Disable built-in tools so only run_code and read_workspace are available:"
            )
          );
          ldCard.appendChild(
            createCodePanel(
              plan.platform === "windows" ? "Windows PowerShell (cmd /c --% preserves empty quotes)" : "Shell",
              ld.baseCmd
            )
          );
          ldCard.appendChild(
            el(
              "p",
              "step-note",
              "Stricter — also exclude other MCP servers and refuse any unlisted tool without prompting (save only-vestibule.json below because --strict-mcp-config ignores --scope user):"
            )
          );
          ldCard.appendChild(
            createCodePanel(
              plan.platform === "windows" ? "Windows PowerShell (strict lockdown)" : "Shell (strict lockdown)",
              ld.strictCmd
            )
          );
          ldCard.appendChild(createCodePanel("only-vestibule.json", ld.onlyVestibuleJson));
          ldCard.appendChild(el("p", "step-note-warn", ld.caveat));
        }
        outputEl.appendChild(ldCard);
      }

      // Step 5: Verify & watch live
      const verifyCard = el("div", "step-block");
      const verifyHead = el("div", "step-header");
      verifyHead.appendChild(el("h3", "step-title", "5. Verify & watch live"));
      verifyCard.appendChild(verifyHead);
      verifyCard.appendChild(
        el(
          "p",
          "step-note",
          "Ask your agent to run print(1) via run_code. Then verify independently from your terminal with the audit log or launch the live read-only inspector:"
        )
      );
      verifyCard.appendChild(createCodePanel("Terminal", plan.verifyCmd));
      outputEl.appendChild(verifyCard);
    }

    const toggleInputs = form.querySelectorAll('input[type="radio"], input[type="checkbox"]');
    for (let i = 0; i < toggleInputs.length; i += 1) {
      toggleInputs[i].addEventListener("change", render);
    }
    const textInputs = form.querySelectorAll('input[type="text"]');
    for (let i = 0; i < textInputs.length; i += 1) {
      textInputs[i].addEventListener("input", render);
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      render();
    });

    render();
  }

  function initDemoLightbox() {
    const trigger = document.getElementById("demo-expand-btn");
    const dialog = document.getElementById("demo-lightbox");
    const closeBtn = document.getElementById("demo-lightbox-close");
    if (!trigger || !dialog || !closeBtn) return;

    trigger.addEventListener("click", () => {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
        closeBtn.focus();
      }
    });

    closeBtn.addEventListener("click", () => {
      if (dialog.open) dialog.close();
    });

    dialog.addEventListener("click", (event) => {
      if (event.target === dialog && dialog.open) {
        dialog.close();
      }
    });

    dialog.addEventListener("close", () => {
      trigger.focus();
    });
  }

  function initInspectorPreview() {
    const tabRuns = document.getElementById("insp-tab-runs");
    const tabWs = document.getElementById("insp-tab-ws");
    const strip = document.getElementById("insp-strip");
    const runsView = document.getElementById("insp-runs-view");
    const wsView = document.getElementById("insp-ws-view");
    const runList = document.getElementById("insp-run-list");
    const runDetail = document.getElementById("insp-run-detail");
    const wsList = document.getElementById("insp-ws-list");
    const wsDetail = document.getElementById("insp-ws-detail");
    if (!tabRuns || !tabWs || !runList || !runDetail || !wsList || !wsDetail) return;

    const SAMPLE_RUNS = [
      {
        id: "e2c8ebceaa1cae1e",
        time: "01:10:43",
        lang: "python",
        outcome: "ran",
        iso: "native",
        wall: "170 ms",
        cpu: "77 ms",
        mem: "12 MiB",
        pids: "2",
        denied: [],
        reason: "",
        preview: "read fake_key.txt + connect 1.1.1.1",
        code:
          'import socket\nKEY = "/tmp/demo-secret/fake_key.txt"\ntry:\n    print(open(KEY).read().strip())\nexcept OSError as e:\n    print("read ->", e.strerror)  # No such file or directory\ntry:\n    socket.create_connection(("1.1.1.1", 80), timeout=3)\nexcept OSError as e:\n    print("net  ->", e.strerror)  # Network is unreachable',
      },
      {
        id: "7a41f0b2c8910e3d",
        time: "01:11:02",
        lang: "python",
        outcome: "ran",
        iso: "native",
        wall: "134 ms",
        cpu: "41 ms",
        mem: "11 MiB",
        pids: "2",
        denied: ["mount", "socket(AF_VSOCK)"],
        reason: "",
        preview: "seccomp probe: mount + AF_VSOCK",
        code:
          'import ctypes, socket\nlibc = ctypes.CDLL("libc.so.6", use_errno=True)\nlibc.mount(b"none", b"/tmp", b"tmpfs", 0, None)  # -> EPERM (logged)\nsocket.socket(40, socket.SOCK_STREAM)           # AF_VSOCK -> EPERM',
      },
      {
        id: "9f03c7e1b2448a10",
        time: "01:11:19",
        lang: "bash",
        outcome: "timed_out",
        iso: "native",
        wall: "10,004 ms",
        cpu: "7,480 ms (throttled)",
        mem: "6 MiB",
        pids: "2",
        denied: [],
        reason: "",
        preview: "while :; do :; done",
        code: "while :; do :; done\n# stopped automatically from outside at the 10 s timeout",
      },
      {
        id: "b120d8e44109f2cc",
        time: "01:11:40",
        lang: "python",
        outcome: "blocked",
        iso: "none (nothing ran)",
        wall: "0 ms",
        cpu: "—",
        mem: "—",
        pids: "—",
        denied: [],
        reason: "code exceeds maximum size cap (256 KiB)",
        preview: "Blocked: oversized payload (310 KiB)",
        code: "# Rejected before starting any sandbox.",
      },
    ];

    const SAMPLE_WS = [
      {
        name: "report.txt",
        kind: "file",
        meta: "48 B · regular file",
        body: "sandbox output ok\nrows processed: 128\nstatus: clean\n",
      },
      {
        name: "evil_link",
        kind: "refused",
        meta: "symlink → ~/.ssh/id_ed25519",
        body:
          "Refused: symbolic links are never followed.\n\nBoth read_workspace and vestibule-mcp ui refuse symlinks and path escapes outside /workspace.",
      },
    ];

    let activeTab = "runs";
    let selectedRunId = SAMPLE_RUNS[0].id;
    let selectedWsName = SAMPLE_WS[0].name;

    function outcomeBadgeClass(outcome) {
      if (outcome === "ran") return "insp-badge insp-badge-ok";
      if (outcome === "timed_out") return "insp-badge insp-badge-warn";
      return "insp-badge insp-badge-danger";
    }

    function addMetric(parent, label, value) {
      const box = el("div", "");
      box.appendChild(document.createTextNode(label + ": "));
      box.appendChild(el("strong", "", value));
      parent.appendChild(box);
    }

    function renderInspector() {
      const isRuns = activeTab === "runs";
      tabRuns.classList.toggle("is-active", isRuns);
      tabRuns.setAttribute("aria-pressed", isRuns ? "true" : "false");
      tabWs.classList.toggle("is-active", !isRuns);
      tabWs.setAttribute("aria-pressed", !isRuns ? "true" : "false");

      if (strip) strip.classList.toggle("is-hidden", !isRuns);
      if (runsView) runsView.classList.toggle("is-hidden", !isRuns);
      if (wsView) wsView.classList.toggle("is-hidden", isRuns);

      clearNode(runList);
      for (let i = 0; i < SAMPLE_RUNS.length; i += 1) {
        const r = SAMPLE_RUNS[i];
        const btn = el("button", r.id === selectedRunId ? "insp-row is-active" : "insp-row");
        btn.type = "button";
        const top = el("div", "insp-row-top");
        const meta = el("span", "");
        meta.appendChild(document.createTextNode(r.time + " · "));
        meta.appendChild(el("span", "insp-row-lang", r.lang));
        top.appendChild(meta);
        top.appendChild(el("span", outcomeBadgeClass(r.outcome), r.outcome));
        btn.appendChild(top);
        btn.appendChild(el("div", "insp-row-preview", r.preview));
        btn.addEventListener("click", () => {
          selectedRunId = r.id;
          renderInspector();
        });
        runList.appendChild(btn);
      }

      const curRun = SAMPLE_RUNS.find((x) => x.id === selectedRunId) || SAMPLE_RUNS[0];
      clearNode(runDetail);
      const head = el("div", "insp-detail-head");
      head.appendChild(el("span", "insp-detail-id", curRun.id));
      head.appendChild(el("span", outcomeBadgeClass(curRun.outcome), curRun.outcome));
      head.appendChild(
        el(
          "span",
          curRun.iso === "native" ? "insp-badge insp-badge-ok" : "insp-badge",
          "isolation: " + curRun.iso
        )
      );
      runDetail.appendChild(head);
      runDetail.appendChild(el("pre", "insp-code", curRun.code));

      if (curRun.denied.length > 0) {
        runDetail.appendChild(
          el("p", "insp-note-warn", "blocked syscalls: " + curRun.denied.join(", "))
        );
      }
      if (curRun.reason) {
        runDetail.appendChild(el("p", "insp-note-danger", "reason: " + curRun.reason));
      }

      const metrics = el("div", "insp-metrics");
      addMetric(metrics, "wall_ms", curRun.wall);
      addMetric(metrics, "cpu_ms", curRun.cpu);
      addMetric(metrics, "mem_peak", curRun.mem);
      addMetric(metrics, "pids_peak", curRun.pids);
      runDetail.appendChild(metrics);
      runDetail.appendChild(
        el(
          "p",
          "insp-caveat",
          "Note: kernel usage counts the whole sandbox, including its setup and file cache."
        )
      );

      clearNode(wsList);
      for (let i = 0; i < SAMPLE_WS.length; i += 1) {
        const f = SAMPLE_WS[i];
        const btn = el("button", f.name === selectedWsName ? "insp-row is-active" : "insp-row");
        btn.type = "button";
        const top = el("div", "insp-row-top");
        top.appendChild(el("span", "insp-row-lang", "/workspace/" + f.name));
        top.appendChild(
          el(
            "span",
            f.kind === "refused" ? "insp-badge insp-badge-danger" : "insp-badge",
            f.kind
          )
        );
        btn.appendChild(top);
        btn.appendChild(el("div", "insp-row-sub", f.meta));
        btn.addEventListener("click", () => {
          selectedWsName = f.name;
          renderInspector();
        });
        wsList.appendChild(btn);
      }

      const curWs = SAMPLE_WS.find((x) => x.name === selectedWsName) || SAMPLE_WS[0];
      clearNode(wsDetail);
      const wsHead = el("div", "insp-detail-head");
      wsHead.appendChild(el("span", "insp-detail-id", "/workspace/" + curWs.name));
      wsHead.appendChild(
        el(
          "span",
          curWs.kind === "refused" ? "insp-badge insp-badge-danger" : "insp-badge",
          curWs.meta
        )
      );
      wsDetail.appendChild(wsHead);
      wsDetail.appendChild(
        el(
          "pre",
          curWs.kind === "refused" ? "insp-code insp-code-refused" : "insp-code",
          curWs.body
        )
      );
    }

    tabRuns.addEventListener("click", () => {
      activeTab = "runs";
      renderInspector();
    });
    tabWs.addEventListener("click", () => {
      activeTab = "ws";
      renderInspector();
    });

    renderInspector();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initHeroCopy();
    initDemoLightbox();
    initInspectorPreview();
    initChamberTabs();
    initWizard();
  });
})();
