
# Vestibule — Plan & Roadmap

The concrete plan: why this project, the milestones with acceptance criteria, cross-platform strategy, adoption, and résumé framing. For *how* it works internally, see `ARCHITECTURE.md`. For *how to start*, see `GETTING_STARTED.md`.

---

## Why this project

The agentic-AI job market is real and growing (agent job postings up ~280% YoY), but "a ReAct loop that calls some tools" is now commodity — the 2026 equivalent of a todo app. What stands out is the **infrastructure and rigor around** the agent: evaluation, reliability, isolation, and enterprise-grade deployment. Vestibule is a piece of that infrastructure, at the intersection the market rewards most: **AI + systems + security.**

### Why it's *this* builder's project

| Existing asset                                                                                                 | How it becomes the differentiator                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **mysh** — Unix shell in C: fork/exec, process mgmt, pipes, TCP wire protocol, race-condition debugging | The warden*is* fork/exec + pipe plumbing + process-group signaling + `wait4` reaping, with namespace flags added. Same primitives, explainable from experience. |
| **Local Explorer** — MCP config on Windows, Claude Code experience                                      | Knows the MCP client side and the pain of setup → builds a*good* install UX.                                                                                     |
| **Homega ERP role** (Python + AWS/GCP + LLM assistant)                                                   | Rehearses the exact "run AI-generated automation safely" problem. Direct résumé synergy.                                                                          |

Almost every student building an agent project is gluing APIs together in Python; ~none can explain how tool execution is isolated at the kernel level. That's the moat.

### How it's differentiated from existing tools

Several "agent sandbox / MCP gateway" projects exist (some even named "Airlock"), but most are **policy/allowlist gateways** or **cloud-backed** sandboxes. Vestibule's angle is **local + free + real kernel-level isolation on your own machine**. Keep that angle sharp; it's the reason to exist.

---

## Locked decisions (quick reference)

| Decision                         | Choice                                                         |
| -------------------------------- | -------------------------------------------------------------- |
| Product name                     | **Vestibule** (`vestibule-mcp` on PyPI)                |
| MCP server language              | **Python** (official `mcp` SDK)                        |
| Warden (isolation core) language | **Python + ctypes** (direct syscalls)                    |
| seccomp layer                    | **Required for native** (our own ctypes binding over libseccomp; allowlist derived from Docker's) |
| License                          | **MIT**                                                  |
| Ownership                        | **Solo**                                                 |

## Milestones

Build in this order. The ordering is deliberate — do not build the native warden before the container backend.

### Milestone 0 — Skeleton that runs (unsafely) · ~weekend 1

Prove the plumbing end-to-end with zero isolation.

- Python MCP server (official SDK) exposing `run_code`.
- `NaiveBackend`: runs code via `subprocess` with a timeout. **No isolation** — plumbing only.
- Registered with Claude Code / a live agent.

**Acceptance:** a live agent calls `run_code` and you see `print("hi")` execute and the output come back. Smoke test passes. `ruff`/`mypy` clean.

**All starter code for this milestone is in the M0 walkthrough, now archived at `docs/archive/M0-scaffold-walkthrough.md`.**

### Milestone 1 — Container backend + cross-platform floor · ~week 1

Make it *usable by ~80% of the audience* on day one.

- `ContainerBackend`: run code in a throwaway Docker/Podman container with `--network none`, `--memory`, `--cpus`, `--pids-limit`, read-only rootfs, non-root user, workspace bind-mount.
- Capability detection at startup: native on Linux (M2), container elsewhere.
- `read_workspace` tool with strict path-jailing (no escape above the workspace dir).

**Acceptance:** on macOS/Windows (with Docker) an agent runs code that is genuinely network-isolated and memory-capped; a fork bomb and a memory bomb are both contained; `read_workspace` cannot read outside the workspace. Result reports `isolation: container`.

**Detailed contract:** `docs/plans/M1/M1-container-backend.md` — written after the pre-implementation adversarial review (`docs/reviews/M1-codex-adversarial-review.md`); its sharpened acceptance criteria supersede the list above.

**Status (2026-10-14):** ✅ accepted. Built in steps 1–7; the acceptance suite, `tests/test_acceptance.py`, passes all 14 criteria on Windows with Docker Desktop, and the live-agent check from a Claude Code session passed (results in `docs/HISTORY.md`).

### Milestone 2 — Native Linux isolation core · ~weeks 2–3 · **the differentiator**

The warden, in Python + ctypes. See `ARCHITECTURE.md` for the full lifecycle.

- A fresh helper per run: `unshare(NEWUSER|NEWPID)` → identity uid/gid map → `fork()` init (PID 1), which unshares `NEWNS|NEWNET|NEWUTS|NEWIPC|NEWCGROUP`.
- Mount fresh `/proc`; `pivot_root` into the pinned images, unpacked once by `vestibule-mcp setup`; writable `/tmp` (tmpfs) + bind-mounted workspace.
- cgroups v2: `memory.max`, `cpu.max`, `pids.max` (filesystem writes).
- `no_new_privs` + a required seccomp allowlist (our own libseccomp binding, derived from Docker's profile) + drop capabilities.
- A fresh helper process per run makes the blocking syscalls, so they never stall the async server.
- **Benchmark cold-start latency vs. the container backend.** Measured on WSL2: native 82 ms median, container 392 ms (2026-10-31). On 2026-11-06 the same host measured 118 ms and 895 ms with the code of `03f0fb7`, and the code from before M3 measured the same, so the host got slower. The container path there goes through Docker Desktop's VM, so the README quotes both numbers with that caveat, not a ratio.

**Acceptance:** on Linux, code runs with no host filesystem visibility (a canary planted at an absolute host path can't be read), no network, enforced mem/cpu/pids caps, and denied syscalls reported. Result reports `isolation: native`. Cold-start benchmark recorded.

**Detailed contract:** `docs/plans/M2/M2-native-warden.md`; its 17 acceptance criteria supersede the list above.

**Status (2026-10-31):** ✅ accepted. Built in steps 0–7; the acceptance suite, `tests/test_native_acceptance.py`, covers criteria 1–13 through the server on WSL2 Ubuntu 26.04, and the live-agent check from Linux Claude Code in WSL passed (results in `docs/HISTORY.md`).

### Milestone 3 — Audit log + resource reporting · ~week 3 · the observability story

- Structured JSONL audit trail of every execution (request, code hash, exit, resource high-water marks, denied syscalls).
- `memory.peak` / `cpu.stat` surfaced in every result.
- A `vestibule-mcp audit` command to pretty-print what agents have been doing.

**Acceptance:** every run appends one audit record; `vestibule-mcp audit` prints a readable history; results include real usage numbers.

**Detailed contract:** `docs/plans/M3/M3-audit-usage.md`; its 10 acceptance criteria supersede the list above. It amends "every result" for containers (A1): a container result reports `wall` only, because its kernel numbers could reach the server only through the guest's own output, which the guest can fake.

**Status (2026-11-07):** ✅ accepted. Built in steps 1–5, step 5 in two parts with a fix between them; the acceptance suite, `tests/test_audit_acceptance.py`, covers criteria 1–6 through the server on WSL2 Ubuntu 26.04 and Windows, and the live checks from Claude Code on Linux (native) and Windows (container) passed (results in `docs/HISTORY.md`).

### Milestone 4 — Polish for adoption · ~week 4 · treat as a real feature

- One-command install (`pipx install vestibule-mcp`) + copy-paste MCP config for Claude Code & Cursor.
- README with a **20-second demo GIF**: an agent tries `rm -rf /` (or reads `~/.ssh`) and is safely contained.
- **Ruthless first-run test:** fresh machine → working in < 2 minutes, or people bounce.
- `SECURITY.md`: honest threat model — what it does and does **not** protect against.

**Acceptance:** a stranger can install and run it in under 2 minutes on a clean machine following only the README.

---

## Cross-platform strategy (non-negotiable for adoption)

| Platform                                     | Backend                                      | Isolation quality             |
| -------------------------------------------- | -------------------------------------------- | ----------------------------- |
| Linux (native)                               | warden: namespaces + cgroups + seccomp       | Strongest — the headline     |
| Linux (no privileges / no cgroup delegation) | rootless Podman, or reduced-guarantee native | Strong / degraded (report it) |
| macOS                                        | container (Docker / Podman)                  | Good                          |
| Windows                                      | container (Docker Desktop); a server run inside WSL2 gets native, and a bridge for Windows clients is an M4 candidate | Good                          |

**Rule:** the tool must *run* everywhere even if best-in-class isolation is Linux-only. Degrade loudly (report `isolation:` honestly), never fail silently.

---

## Adoption playbook (~⅓ of the project; budget for it like a feature)

- **README:** demo GIF above the fold, one-line install, the "why" in two sentences, honest security scope.
- **Launch day:** Show HN; r/ClaudeAI, r/LocalLLaMA, r/mcp; X; the MCP/Anthropic Discord. Submit to every MCP registry and `awesome-mcp` list.
- **Seed real use:** wire it into your own Local Explorer or a demo agent so there's a living example.
- **Engage:** respond to every issue fast in month 1 — early responsiveness drives stars and word of mouth.
- **Realistic expectation:** a well-executed launch commonly lands 50–300 stars; breakouts are partly timing luck. Even ~150 stars + external issues is far stronger résumé evidence than any solo demo.

---

## Résumé bullets (draft — fill in real numbers)

- *Built and shipped an open-source MCP server providing kernel-isolated code execution for AI agents (Linux user/mount/pid/net namespaces, cgroups v2, seccomp-bpf), adopted by N developers (X GitHub stars); native sandbox cold start ~118 ms median vs. ~895 ms for the Docker-based backend on the same host (Docker Desktop on WSL2, measured 2026-11-06 at `03f0fb7`).*
- *Designed a layered "defense-in-depth" threat model for autonomous LLM code execution, mitigating prompt-injection-driven filesystem and network abuse; documented syscall-level guarantees in a public security spec.*

Every N/X/Y must be measured and defensible — interview-proof.

---

## Remaining manual checks before public launch

- `pypi.org/project/vestibule` (and `vestibule-mcp`) → confirm 404 / availability.
- GitHub repo/org name, or use `<your-handle>/vestibule`.
- Domain (optional).
