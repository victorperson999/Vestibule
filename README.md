# Vestibule

**Run untrusted AI-agent code safely — on your own machine, for free, with nothing sent to a cloud.**

A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server.

> A *vestibule* is the small sealed entry chamber you pass through between outside and inside. Vestibule sits between an AI agent and your host machine: the agent's generated code runs *inside* the chamber, never touching the real system.

---

## Why use Vestibule?

**The problem.** Agents no longer just suggest code — they write it and execute it, autonomously, in a loop. Running model-generated code on your real machine is the dangerous part: a prompt-injected instruction can exfiltrate secrets (`~/.ssh`, `~/.aws`, `.env`), a hallucinated argument can delete the wrong directory, a runaway loop can eat the machine. That's the *what*: untrusted agent code needs somewhere safe to run.

**The existing answer.** Safe execution is already a product category — hosted sandboxes like E2B and Daytona solve it well, but on their terms: you pay per usage, every execution adds a network round-trip to the agent loop, and every line of generated code (plus whatever data it reads) ships to a third party's infrastructure.

**Vestibule's reason to exist** is refusing all three of those costs at once:

- **Local** — execution is a process spawn, not an HTTP call. No round-trip in the agent loop; works offline, on a plane, behind a corporate firewall.
- **Free** — open source (MIT), no account, no metering, no per-second bill deciding how often your agent gets to run code.
- **No cloud** — your agent's code and the data it touches never leave your machine. There is no third party to trust, to breach, or to leak.

The isolation underneath is real kernel-level machinery — Linux namespaces, cgroups v2, seccomp, the same mechanisms rootless containers are built on — not a best-effort wrapper. (See **Status** below for what exists today.)

## What it is

Vestibule is an open-source [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any AI coding agent — Claude Code, Claude Desktop, Cursor, or anything that speaks MCP — a `run_code` tool that executes the model's generated code inside an isolated sandbox instead of on your actual machine: no network access by default, resource limits, a filesystem the guest can't see out of, and honest reporting of what protection was actually applied.

## Status

> ⚠️ **Early development — do not use this to run untrusted code yet.**
> The only backend that exists today applies **no isolation** (and says so in every result: `isolation: none`). The isolation layers are designed and are being built next. Not yet published to PyPI.

| Milestone | What                                                                                         | Status                                                                                                 |
| --------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| M0        | MCP server +`run_code` end-to-end (naive backend, **no isolation**)                  | ✅ done                                                                                                |
| M1        | Container backend (Docker/Podman) — the cross-platform isolation floor, +`read_workspace` | 🔜 designed, next up — see[`docs/plans/M1-container-backend.md`](docs/plans/M1-container-backend.md) |
| M2        | Native Linux isolation (namespaces + cgroups v2 + seccomp, no root required)                 | planned                                                                                                |
| M3        | Audit log + resource reporting                                                               | planned                                                                                                |
| M4        | Install/onboarding polish,`SECURITY.md` threat model                                       | planned                                                                                                |

## How it works (design)

Two halves: the **MCP server** decides *whether/what* (validation, limits, schemas, audit), and a **warden** backend decides *how* to isolate and run. Every result carries an `isolation:` field stating what actually protected the run — `native`, `container`, or `none` — and the tool never claims protection that wasn't applied.

Honest scope, stated up front: this is namespace/container isolation sharing the host kernel, **not** a hardened VM boundary. The goal is to make the common, realistic agent risks — prompt-injected exfiltration, destructive commands, resource exhaustion — structurally impossible, not to defend against kernel 0-days. The full threat model ships as `SECURITY.md` in M4.

## Documentation

| File                                                                        | What's in it                                                                               |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)                             | Deep technical design: the warden lifecycle, layered isolation, MCP server design.         |
| [`docs/PLAN.md`](docs/PLAN.md)                                             | Roadmap: milestones 0–4 with acceptance criteria.                                         |
| [`docs/plans/M1-container-backend.md`](docs/plans/M1-container-backend.md) | The detailed M1 contract (container profile,`read_workspace` jail, acceptance criteria). |
| [`docs/reviews/`](docs/reviews/)                                           | Pre-implementation adversarial design reviews.                                             |
| [`CLAUDE.md`](CLAUDE.md)                                                   | Invariants and operating rules for AI-assisted development of this repo.                   |
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md)                       | Dev setup and the original M0 scaffold walkthrough.                                        |

## License

[MIT](LICENSE)
