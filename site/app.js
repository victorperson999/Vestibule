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

  document.addEventListener("DOMContentLoaded", () => {
    initHeroCopy();
    initChamberTabs();
    initWizard();
  });
})();
