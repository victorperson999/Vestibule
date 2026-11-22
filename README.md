# Vestibule

**Run untrusted AI-agent code in a sandbox on your own machine: free, local, and with no hosted sandbox service in the loop.**

A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/victorperson999/Vestibule/blob/main/LICENSE) [![Python ≥ 3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](https://www.python.org/downloads/) [![MCP Server](https://img.shields.io/badge/MCP-Server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io) [![Status: Early Development](https://img.shields.io/badge/status-early%20development-orange.svg)](https://github.com/victorperson999/Vestibule#status) [![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/victorperson999/badge.svg)](https://hub.xerj.org/contributors/victorperson999)

![Terminal recording. A short program runs twice. On the machine itself it reads a fake key file and connects to 1.1.1.1. Run through Vestibule, the same program reports "No such file or directory" for the file and "Network is unreachable" for the connection, and the result ends with "isolation: native".](https://raw.githubusercontent.com/victorperson999/Vestibule/main/docs/demo-2.gif)

*The same script runs directly on the host (reads a fake secret and reaches `1.1.1.1`), then inside Vestibule (blocked on both, reporting `isolation: native` under WSL2).*

> A *vestibule* is a small sealed entry chamber between outside and inside. Vestibule sits between an AI agent and your machine: code the agent sends to `run_code` executes *inside* the chamber, with `/workspace` as its only doorway.

```text
                                ╔═════════════════════════════╗
┌──────────────┐                ║          VESTIBULE          ║                ┌──────────────────┐
│              │    run_code    ║                             ║    sealed:     │   your machine   │
│   AI agent   │ ─────────────► ║   generated code runs here  ║ ────── ✕ ────►│  files, secrets,  │
│  (via MCP)   │ ◄───────────── ║                             ║ no direct path │  network, shell  │
│              │  output only   ║  no network · no host files ║                │   — kept out     │
└──────────────┘                ║  capped cpu · memory · /tmp ║                └──────────────────┘
                                ╚══════════════╤══════════════╝
                                               │
                  the one doorway: /workspace (a dedicated folder for files)
```

Only code sent to `run_code` runs inside the chamber — your agent's own shell and file tools still run on your machine unless you restrict them ([how](https://github.com/victorperson999/Vestibule#lock-down-your-agent)).

---

## Why Vestibule?

AI coding agents don't just suggest code anymore — they write and execute it autonomously in a loop. Running model-generated code directly on your host is the risky part: a prompt injection can read `~/.ssh` or `.env`, a bad path can wipe the wrong folder, or a runaway loop can starve the machine.

Hosted sandboxes (like E2B or Daytona) solve this, but you pay per second, add a network round-trip to every run, and hand your code to a third party. Vestibule avoids all three:

- **Local** — a process spawn, not an HTTP call. No network needed after `setup`.
- **Free** — MIT-licensed, no account, no metering, no usage bill.
- **No sandbox vendor** — no telemetry and nothing sent anywhere by Vestibule itself. (Your agent still talks to its model provider as usual.)

**What it gives your agent:** an MCP server with two tools for Claude Code, Cursor, Claude Desktop, or any MCP client:

- **`run_code`** — runs Python 3.12, Node 22, or Bash in a fresh, network-less, resource-capped sandbox (Linux namespaces, cgroups v2, and seccomp natively on Linux; a locked-down container where native isolation isn't available).
- **`read_workspace`** — safely reads files a run writes to `/workspace`, refusing symlinks.

> ⚠️ **Early development — do not rely on this for hostile code yet.** See [Isolation and security](https://github.com/victorperson999/Vestibule#isolation-and-security) and [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md) for what is and isn't protected.

## Quick start

You need an MCP client (examples below use [Claude Code](https://claude.com/claude-code)) and [uv](https://docs.astral.sh/uv/).

| Platform | Prerequisite | Isolation you get |
| --- | --- | --- |
| Linux with systemd (Debian, Fedora, Arch, WSL2) | `curl` (minimal Debian also needs `ca-certificates` and `dbus-user-session`) | `native` (no Docker needed; measured on WSL2 & Debian 13; arm64 untested) |
| Windows | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), running | `container` (measured; use WSL2 for `native`) |
| Ubuntu 23.10+ (host or VM, not WSL2) | `curl` + [Docker Engine from Docker's apt repo](https://docs.docker.com/engine/install/ubuntu/) | `container` (Ubuntu restricts unprivileged user namespaces via AppArmor) |
| macOS | Docker Desktop for Mac, running | `container` (expected — untested on macOS) |

Details on where native isolation works: [Where native works](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#where-native-works).

### 1. Platform prerequisites

- **Linux (Debian / Fedora / Arch / WSL2):** ensure `curl` is installed. On a minimal Debian, run `sudo apt install ca-certificates curl dbus-user-session && sudo reboot`.
- **Ubuntu 23.10+ (host/VM):** install `curl` and Docker Engine from Docker's apt repo, then run `sudo usermod -aG docker $USER` and log out/in. **Do not use the Docker snap** — it rejects `no-new-privileges`, which Vestibule requires. *(Note: membership in the `docker` group grants root-equivalent access on Linux.)*
- **Windows / macOS:** install and start Docker Desktop. *(On Windows, if Docker Desktop asks for Virtual Machine Platform, run `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` in an admin PowerShell and reboot.)*

### 2. Install `uv` (then open a new terminal)

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 3. Install Vestibule, run `setup`, and connect Claude Code

```bash
uv tool install vestibule-mcp
uv tool update-shell           # Windows: if this updates PATH, open a new terminal
vestibule-mcp setup            # downloads ~123 MB of pinned runtimes and checks isolation
claude mcp add --scope user vestibule -- vestibule-mcp
```

`setup` prints `ready in this terminal's environment: isolation native` (or `isolation container`). For Cursor, Claude Desktop, or macOS GUI clients, see [Connect other clients](https://github.com/victorperson999/Vestibule#connect-other-clients).

### 4. Verify it works

Ask your agent to run `print(1)` with Vestibule's `run_code` tool. A native run returns:

```text
exit_code: 0
stdout:
1

usage: wall=120ms, cpu=37ms, mem_peak=13MiB, pids_peak=2 (the whole sandbox, including its setup and file cache)
isolation: native
```

Under `container`, `usage:` reports `wall` time only (cgroup peaks aren't readable from outside a container) and ends with `isolation: container`.

Because agents often fold or summarize tool output, you can verify directly from your terminal with the audit log:

```bash
vestibule-mcp audit
# 2026-11-20T23:03:35.569Z  python  ran        isolation=native exit=0 wall=120ms cpu=37ms mem_peak=13MiB  print(1)
```

## Connect other clients

Add this entry to your client's MCP config (merge into `mcpServers` if it already exists), then fully restart the client:

- **Cursor:** `~/.cursor/mcp.json` *(tested on Windows)*
- **Claude Desktop:** `%APPDATA%\Claude\claude_desktop_config.json` on Windows *(tested)*, or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS *(untested)*

```json
{
  "mcpServers": {
    "vestibule": { "command": "vestibule-mcp" }
  }
}
```

GUI clients on macOS (or any client that can't find `vestibule-mcp` / `docker` on `PATH`) need the full binary path (`uv tool dir --bin`) and an explicit `PATH`:

```json
{
  "mcpServers": {
    "vestibule": {
      "command": "/Users/you/.local/bin/vestibule-mcp",
      "env": { "PATH": "/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

## Lock down your agent

**Only code sent to `run_code` is sandboxed.** If your agent still has its own bash or file tools enabled, it can use them directly on your machine.

To run a **sandbox-only Claude Code session** where `run_code` and `read_workspace` are the only tools available:

```bash
# Disable built-in tools, allow only Vestibule:
claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
```

```powershell
# Windows PowerShell drops empty "" args — wrap with cmd:
cmd /c --% claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
```

To also exclude your other MCP servers and refuse any unlisted tool without prompting, save the `mcpServers` JSON above as `only-vestibule.json` and add `--strict-mcp-config --mcp-config only-vestibule.json --permission-mode dontAsk`:

```bash
claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace --strict-mcp-config --mcp-config only-vestibule.json --permission-mode dontAsk
```

*Note:* Claude Code plugin hooks still run even with `--strict-mcp-config` — disable plugins (`/plugin`) if you don't want hooks running. Other MCP clients have their own tool-restriction settings.

## Isolation and security

Four practical boundaries to keep in mind (full threat model and test citations in [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md)):

- **Not a VM.** On Linux, the sandbox shares your host kernel, so a kernel exploit could in principle escape it. On Windows and macOS, Docker Desktop runs containers inside its own Linux VM.
- **The workspace is writable and uncapped.** Code can create, edit, or delete anything in `/workspace` (the rootfs is read-only and `/tmp` is capped, but workspace disk usage is not). Set `VESTIBULE_WORKSPACE_RO=1` to mount it read-only.
- **Treat `/workspace` like an untrusted download.** Sandboxed code can create real symlinks inside `/workspace` pointing to host files like `~/.ssh/id_ed25519`. Vestibule's `read_workspace` refuses symlinks; don't point `VESTIBULE_WORKSPACE` at an existing project folder or let other tools follow links inside it blindly.
- **Run output goes back to your agent.** Anything printed to stdout/stderr is read by the agent and sent to its model provider.

Every `run_code` result states honestly what protection actually applied:

| `isolation:` | What it means |
| --- | --- |
| `native` | Linux namespaces, `pivot_root` jail, seccomp allowlist, and all cgroup v2 resource limits applied. |
| `container` | Locked-down container profile (no network, all caps dropped, read-only rootfs, non-root, resource caps) handed to Docker (or experimental Podman). |
| `native-degraded (…)` / `container-degraded (…)` | Isolated, but some resource limits couldn't be applied or verified — the parentheses say which and why. |
| `none` | No isolation claimed (`VESTIBULE_BACKEND=naive` for dev only, or an unverified failure explained in parentheses). |
| `Blocked:` (no `isolation:` line) | Nothing ran. The message tells you what to fix and whether to retry or restart. |

## Configuration and auditing

Set options in the `env` block of your MCP client config and restart the client. Invalid settings never fail silently — the server starts and replies `Blocked:` naming the setting to fix.

Common settings (full list in [`docs/REFERENCE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#configuration)):

- `VESTIBULE_WORKSPACE` — host folder mounted at `/workspace` (default: `~/.vestibule/workspace`)
- `VESTIBULE_WORKSPACE_RO=1` — mount `/workspace` read-only
- `VESTIBULE_BACKEND` — force `native`, `container`, or `naive` (dev only)
- `VESTIBULE_MEM_MB` / `VESTIBULE_CPU_PCT` — memory and CPU caps
- `VESTIBULE_DEFAULT_TIMEOUT_S` — default per-run timeout

**Auditing:** every `run_code` call that reaches Vestibule is logged to `~/.vestibule/audit/` (`VESTIBULE_AUDIT_DIR`), storing the code (truncated at 4 KiB), outcome, isolation, usage, and output sizes (never output contents). Inspect recent runs with `vestibule-mcp audit` (last 20) or `vestibule-mcp audit 50`; output is terminal-escaped. Details in [Usage numbers and the audit log](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#usage-numbers-and-the-audit-log).

**About `setup`:** downloads ~123 MB of digest-pinned `python:3.12-slim` and `node:22-slim` layers/images once (unpacked to `~/.vestibule/rootfs` for `native`, or pulled into Docker for `container`). Tool calls never pull from the network, and Vestibule never needs root (it refuses to run as root on Linux/macOS). Details and custom-image rules: [What `setup` does, in detail](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#what-setup-does-in-detail). To pull the pinned Docker images manually:

```bash
docker pull docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
docker pull docker.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
```

### Common first-run issues

- **`uv` or `vestibule-mcp` not found:** open a new terminal (on Windows, run `uv tool update-shell` first).
- **`CERTIFICATE_VERIFY_FAILED` during `setup`:** install `ca-certificates` (minimal Debian).
- **`native-degraded (… no systemd user bus)`:** install `dbus-user-session` and reboot.
- **`daemon not reachable`:** start Docker; on Linux, add yourself to the `docker` group (`sudo usermod -aG docker $USER`), log out/in, and restart your client.
- **Everything else:** `Blocked:` replies name the fix; see [`docs/GETTING_STARTED.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md#troubleshooting) for the full list.

### Uninstall

Quit your MCP client first, and copy out anything you want to keep from `~/.vestibule` (workspace + audit log):

```bash
claude mcp remove --scope user vestibule                # or remove from your client's mcp config
uv tool uninstall vestibule-mcp
chmod -R u+rwX ~/.vestibule && rm -rf ~/.vestibule      # Windows: Remove-Item -Recurse -Force $HOME\.vestibule
# only if setup pulled the Docker images:
docker image rm docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b docker.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
```

## How it works

Vestibule splits into two halves: a clean async **MCP server** (argument validation, caps, schemas, audit logging — untrusted code never runs in the server process) and a pluggable **warden** backend that handles isolation and execution. On Linux with unprivileged user namespaces and systemd, the native warden isolates each run directly with user/mount/pid/net namespaces, `pivot_root`, cgroups v2, and a `libseccomp` allowlist — no Docker and no root. Everywhere else, the container warden runs a locked-down throwaway container. See [`docs/ARCHITECTURE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/ARCHITECTURE.md) for the full design.

## Status

| Milestone | Scope | Status |
| --- | --- | --- |
| M0 | MCP server + `run_code` plumbing (`naive` backend, no isolation) | ✅ done |
| M1 | Container backend (Docker/Podman) + `read_workspace` path jail | ✅ accepted ([contract](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M1/M1-container-backend.md)) |
| M2 | Native Linux warden (namespaces + cgroups v2 + seccomp, unprivileged) | ✅ accepted ([contract](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M2/M2-native-warden.md)) |
| M3 | External resource usage reporting + local audit log (`vestibule-mcp audit`) | ✅ accepted ([contract](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M3/M3-audit-usage.md)) |
| M4 | PyPI release (`vestibule-mcp`), `setup` CLI, [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md), docs | ✅ accepted ([plan](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M4/M4-adoption.md)) |

## Documentation

| File | What's in it |
| --- | --- |
| [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md) | Threat model: what Vestibule protects and doesn't, test citations, vulnerability reporting. |
| [`docs/REFERENCE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md) | Full config reference, `setup` internals & pinned digests, native Linux matrix, audit log spec. |
| [`docs/ARCHITECTURE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/ARCHITECTURE.md) | Deep technical design: MCP server, warden lifecycle, layered isolation. |
| [`docs/GETTING_STARTED.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md) | Developer guide: dev install, running checks, troubleshooting. |
| [`docs/PLAN.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/PLAN.md) | Roadmap: milestones M0–M4 and acceptance criteria. |
| [`docs/HISTORY.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/HISTORY.md) | Build story: design decisions, sign-offs, and milestone history. |
| [`docs/BUILD_LOG.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/BUILD_LOG.md) | Regression index: commits, files, guarding tests, and open issues per step. |
| [`docs/plans/`](https://github.com/victorperson999/Vestibule/tree/main/docs/plans) & [`docs/reviews/`](https://github.com/victorperson999/Vestibule/tree/main/docs/reviews) | Per-step plans and pre-implementation adversarial design reviews. |
| [`CLAUDE.md`](https://github.com/victorperson999/Vestibule/blob/main/CLAUDE.md) | Invariants and operating rules for AI-assisted development in this repo. |

## License

[MIT](https://github.com/victorperson999/Vestibule/blob/main/LICENSE)
