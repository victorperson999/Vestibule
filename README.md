# Vestibule

**A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server.**

> A *vestibule* is the small sealed entry chamber you pass through between outside and inside. Vestibule sits between an AI agent and your host machine: the agent's generated code runs *inside* the chamber, never touching the real system.

---

## What it is

Vestibule is an open-source [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any AI coding agent — Claude Code, Claude Desktop, Cursor, or anything that speaks MCP — a `run_code` tool that executes the model's generated code inside an isolated sandbox instead of on your actual machine: no network access by default, resource limits, a filesystem the guest can't see out of, and honest reporting of what protection was actually applied.

The threat model is AI-specific. Agents now write and autonomously execute code, which means prompt injection can make an agent try to exfiltrate secrets (`~/.ssh`, `~/.aws`, `.env`) or run destructive commands, and hallucinated tool arguments can do real damage. Existing answers are either "run it on the host and hope" or a paid cloud sandbox (E2B, Daytona). Vestibule's angle is **local, free, and real kernel-level isolation on your own machine**.

## Status

> ⚠️ **Early development — do not use this to run untrusted code yet.**
> The only backend that exists today applies **no isolation** (and says so in every result: `isolation: none`). The isolation layers are designed and are being built next. Not yet published to PyPI.

| Milestone | What | Status |
|---|---|---|
| M0 | MCP server + `run_code` end-to-end (naive backend, **no isolation**) | ✅ done |
| M1 | Container backend (Docker/Podman) — the cross-platform isolation floor, + `read_workspace` | 🔜 designed, next up — see [`docs/plans/M1-container-backend.md`](docs/plans/M1-container-backend.md) |
| M2 | Native Linux isolation (namespaces + cgroups v2 + seccomp, no root required) | planned |
| M3 | Audit log + resource reporting | planned |
| M4 | Install/onboarding polish, `SECURITY.md` threat model | planned |

## How it works (design)

Two halves: the **MCP server** decides *whether/what* (validation, limits, schemas, audit), and a **warden** backend decides *how* to isolate and run. Every result carries an `isolation:` field stating what actually protected the run — `native`, `container`, or `none` — and the tool never claims protection that wasn't applied.

Honest scope, stated up front: this is namespace/container isolation sharing the host kernel, **not** a hardened VM boundary. The goal is to make the common, realistic agent risks — prompt-injected exfiltration, destructive commands, resource exhaustion — structurally impossible, not to defend against kernel 0-days. The full threat model ships as `SECURITY.md` in M4.

## Documentation

| File | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Deep technical design: the warden lifecycle, layered isolation, MCP server design. |
| [`docs/PLAN.md`](docs/PLAN.md) | Roadmap: milestones 0–4 with acceptance criteria. |
| [`docs/plans/M1-container-backend.md`](docs/plans/M1-container-backend.md) | The detailed M1 contract (container profile, `read_workspace` jail, acceptance criteria). |
| [`docs/reviews/`](docs/reviews/) | Pre-implementation adversarial design reviews. |
| [`CLAUDE.md`](CLAUDE.md) | Invariants and operating rules for AI-assisted development of this repo. |
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) | Dev setup and the original M0 scaffold walkthrough. |

## License

[MIT](LICENSE)
