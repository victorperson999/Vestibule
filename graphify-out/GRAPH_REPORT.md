# Graph Report - .  (2026-07-07)

## Corpus Check
- Corpus is ~32,920 words - fits in a single context window. You may not need a graph.

## Summary
- 307 nodes · 570 edges · 13 communities (10 shown, 3 thin omitted)
- Extraction: 77% EXTRACTED · 23% INFERRED · 0% AMBIGUOUS · INFERRED: 132 edges (avg confidence: 0.76)
- Token cost: 135,814 input · 18,281 output

## Community Hubs (Navigation)
- Config & Selection Machinery
- Container Backend Execution
- Architecture & Design Docs
- Container Integration Tests
- Workspace Path Jail
- Backend Selection & Tests
- MCP Server Handlers
- Input Validation Tests
- Warden Base Abstractions
- Image Pinning & Digests
- Package Init
- PyPI Distribution

## God Nodes (most connected - your core abstractions)
1. `ContainerBackend` - 45 edges
2. `Limits` - 45 edges
3. `BackendSelector` - 33 edges
4. `RunRefusedError` - 24 edges
5. `read_workspace_entry()` - 19 edges
6. `RunResult` - 18 edges
7. `NaiveBackend` - 16 edges
8. `_limits()` - 15 edges
9. `FakeCliProc` - 14 edges
10. `Warden` - 12 edges

## Surprising Connections (you probably didn't know these)
- `Warden` --implements--> `Warden (isolation core: decides how)`  [INFERRED]
  src/vestibule/backends/base.py → docs/ARCHITECTURE.md
- `ContainerBackend` --implements--> `S4-D3: deadline-label orphan reaping`  [EXTRACTED]
  src/vestibule/backends/container.py → docs/plans/M1-step4-lifecycle.md
- `M0: skeleton that runs (unsafely)` --references--> `NaiveBackend`  [EXTRACTED]
  docs/PLAN.md → src/vestibule/backends/naive.py
- `RunRefusedError` --implements--> `S4-D1: bounded semaphore wait, then legible refusal`  [EXTRACTED]
  src/vestibule/backends/base.py → docs/plans/M1-step4-lifecycle.md
- `M1: container backend + cross-platform floor` --references--> `ContainerBackend`  [EXTRACTED]
  docs/PLAN.md → src/vestibule/backends/container.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Layered native isolation stack (warden defense-in-depth)** — docs_architecture_user_namespace, docs_architecture_network_namespace, docs_architecture_pivot_root_jail, docs_architecture_cgroups_v2, docs_architecture_seccomp [EXTRACTED 1.00]
- **M1 contract decision set (D1-D4, D9)** — docs_plans_m1_container_backend_d1_writable_workspace, docs_plans_m1_container_backend_d2_pinned_images, docs_plans_m1_container_backend_d3_tiered_degradation, docs_plans_m1_container_backend_d4_runtime_agnostic, docs_plans_m1_container_backend_d9_script_mount [EXTRACTED 1.00]
- **Honest isolation reporting enforcement across milestones** — claude_honest_isolation_reporting, docs_plans_m1_container_backend_d3_tiered_degradation, docs_plans_m1_step4_lifecycle_s4_d2_missing_runtime_honesty, docs_plans_m1_step5_selection_exit_125_honesty, docs_reviews_m1_step6_adversarial_review_isolation_false_bug [INFERRED 0.85]

## Communities (13 total, 3 thin omitted)

### Community 0 - "Config & Selection Machinery"
Cohesion: 0.06
Nodes (46): _command(), NaiveBackend, Path, Milestone 0 backend: subprocess, NO isolation. Plumbing only — never ship as def, M1 step 5: backend selection & capability probing.  Contract: docs/plans/M1-co, Run the workspace round-trip probe; None on success, else a short reason., None if `<runtime> version` succeeds (CLI present, daemon reachable);     other, The committed verdict: a warden plus what its runs will honestly report. (+38 more)

### Community 1 - "Container Backend Execution"
Cohesion: 0.07
Nodes (27): Two nested timeouts (inner authoritative, outer backstop), D9: read-only /sandbox script delivery, Detached-task cleanup (Codex P2 fix), CRLF guest-script corruption bug, Failed-selection task leak / drain() fix, Event, Process, Semaphore (+19 more)

### Community 2 - "Architecture & Design Docs"
Cohesion: 0.06
Nodes (46): Rule 6: run everywhere, degrade loudly, Rule 4: tool handlers return errors as content, never raise, Golden Rules (project invariants), Rule 5: report isolation honestly, every time, Rule 1: never write to stdout except via the MCP SDK, Rule 2: always run unprivileged, cgroups v2 resource cage, Honest scope: kernel-sharing isolation, not a VM boundary (+38 more)

### Community 3 - "Container Integration Tests"
Cohesion: 0.08
Nodes (26): backend(), _drain(), limits(), _plant(), _ps_names(), ContainerBackend tests (M1 steps 3–4). Marked 'docker' — they need a running da, Plan §6 test 6 — the headline §4.5 behavior: a cancelled request still gets, Plan §6 test 7 — a *running* container with deadline=1 (epoch 1970) is removed: (+18 more)

### Community 4 - "Workspace Path Jail"
Cohesion: 0.13
Nodes (29): Exception, _is_symlink_or_reparse(), _list_dir(), Path, Path jail + read/list logic for the `read_workspace` tool.  Contract: docs/pla, Directory -> listing, file -> content, missing -> 'Not found: …'.      Raises, A requested path was refused by the jail. The message is agent-legible., Validate a user-supplied workspace-relative path; return its components. (+21 more)

### Community 5 - "Backend Selection & Tests"
Cohesion: 0.14
Nodes (30): Run refused before any code executed (e.g. concurrency limit reached).      No, RunRefusedError, BackendSelector, Lazy, cached backend choice (contract §5). One instance per server process., Regression (build-time hang): a FAILED selection must drain its rejected     pr, test_failed_selection_drains_and_leaves_no_tasks(), docker_ok(), _limits() (+22 more)

### Community 6 - "MCP Server Handlers"
Cohesion: 0.25
Nodes (14): _blocked(), call_tool(), _format_result(), get_warden(), _handle_read_workspace(), _handle_run_code(), list_tools(), _main() (+6 more)

### Community 8 - "Warden Base Abstractions"
Cohesion: 0.27
Nodes (7): ABC, Warden interface + result type. Server depends on this abstraction, not on any i, Runs code in *some* level of isolation and reports honestly what it applied., RunResult, Warden, Post-run honesty hook: a container-tier selection that produced a run         w, _ok()

### Community 9 - "Image Pinning & Digests"
Cohesion: 0.22
Nodes (9): D2: pinned official images, no auto-pull, S5-D3: per-run image preflight + --pull never, S6-D1: pin the manifest-list digest, proven through the real code path, S6-D4: proof gate runs under both Docker Desktop image stores, Tag-drift trap: pull-by-tag no longer satisfies a digest preflight, Finding 11: unspecified image strategy, Step-6 Codex adversarial review (pass 2), Medium finding: naive.py spawn failure reports isolation="false" (+1 more)

## Knowledge Gaps
- **4 isolated node(s):** `vestibule-mcp`, `cgroups v2 resource cage`, `CRLF guest-script corruption bug`, `Failed-selection task leak / drain() fix`
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ContainerBackend` connect `Container Backend Execution` to `Config & Selection Machinery`, `Architecture & Design Docs`, `Container Integration Tests`, `Backend Selection & Tests`, `Warden Base Abstractions`?**
  _High betweenness centrality (0.322) - this node is a cross-community bridge._
- **Why does `Limits` connect `Config & Selection Machinery` to `Container Backend Execution`, `Container Integration Tests`, `Backend Selection & Tests`, `Input Validation Tests`, `Warden Base Abstractions`?**
  _High betweenness centrality (0.249) - this node is a cross-community bridge._
- **Why does `RunRefusedError` connect `Backend Selection & Tests` to `Config & Selection Machinery`, `Container Backend Execution`, `Architecture & Design Docs`, `Workspace Path Jail`, `Warden Base Abstractions`?**
  _High betweenness centrality (0.240) - this node is a cross-community bridge._
- **Are the 14 inferred relationships involving `ContainerBackend` (e.g. with `RunRefusedError` and `RunResult`) actually correct?**
  _`ContainerBackend` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `Limits` (e.g. with `RunRefusedError` and `RunResult`) actually correct?**
  _`Limits` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `BackendSelector` (e.g. with `RunRefusedError` and `RunResult`) actually correct?**
  _`BackendSelector` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 20 inferred relationships involving `RunRefusedError` (e.g. with `Limits` and `ContainerBackend`) actually correct?**
  _`RunRefusedError` has 20 INFERRED edges - model-reasoned connections that need verification._