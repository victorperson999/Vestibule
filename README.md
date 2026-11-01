# Vestibule

**Run untrusted AI-agent code safely — on your own machine, for free, with nothing sent to a cloud service.**

A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python ≥ 3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](https://www.python.org/downloads/) [![MCP Server](https://img.shields.io/badge/MCP-Server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io) [![Status: Early Development](https://img.shields.io/badge/status-early%20development-orange.svg)](#status) [![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/victorperson999/badge.svg)](https://hub.xerj.org/contributors/victorperson999)

> A *vestibule* is the small sealed entry chamber you pass through between outside and inside. Vestibule sits between an AI agent and your host machine: the agent's generated code runs *inside* the chamber, never touching the real system.

```text
                                ╔═════════════════════════════╗
┌──────────────┐                ║          VESTIBULE          ║                ┌──────────────────┐
│              │    run_code    ║                             ║    sealed:     │   your machine   │
│   AI agent   │ ─────────────► ║   generated code runs here  ║ ────── ✕ ────►│  files, secrets,  │
│  (via MCP)   │ ◄───────────── ║                             ║ no way through │  network, shell  │
│              │  output only   ║  no network · no host files ║                │   — untouched    │
└──────────────┘                ║  capped cpu · memory · /tmp ║                └──────────────────┘
                                ╚══════════════╤══════════════╝
                                               │
                  the one doorway: /workspace (a dedicated folder for files)
```

---

## Why use Vestibule?

**The problem:** Agents no longer just suggest code — they write it and execute it, autonomously, in a loop. Running model-generated code on your real machine is the dangerous part: a prompt-injected instruction can exfiltrate secrets (`~/.ssh`, `~/.aws`, `.env`), a hallucinated argument can delete the wrong directory, a runaway loop can eat the machine. That's the *what*: untrusted agent code needs somewhere safe to run.

**The answer:** Safe execution is already a product category — hosted sandboxes like E2B and Daytona solve it well, but on their terms: you pay per usage, every execution adds a network round-trip to the agent loop, and every line of generated code (plus whatever data it reads) ships to a third party's infrastructure.

**Vestibule's reason to exist** is refusing all three of those costs at once:

- **Local** — execution is a process spawn, not an HTTP call. No round-trip in the agent loop; works offline, on a plane, behind a corporate firewall.
- **Free** — open source (MIT), no account, no metering, no per-second bill deciding how often your agent gets to run code.
- **No cloud** — your agent's code and the data it touches never leave your machine. There is no third party to trust, to breach, or to leak.

The isolation underneath is real kernel-level machinery — Linux namespaces, cgroups v2, seccomp, the same mechanisms rootless containers are built on — not a best-effort wrapper. (See **Status** below for what exists today.)

## What it is

Vestibule is an open-source [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any AI coding agent — Claude Code, Claude Desktop, Cursor, or anything that speaks MCP — a `run_code` tool that executes the model's generated code inside an isolated sandbox instead of on your actual machine: no network access by default, resource limits, a filesystem the guest can't see out of, and honest reporting of what protection was actually applied.

## Status

> ⚠️ **Early development — do not rely on this for hostile code yet.**
> Two backends are accepted, and every result states the isolation that actually applied.
> - **Native isolation on Linux (M2):** each run executes in a fresh sandbox built from the kernel's own namespaces, cgroup limits and a seccomp allowlist, with no root and no Docker. It passed its 17-criterion acceptance on WSL2 Ubuntu 26.04 and a check from a live Linux Claude Code session.
> - **The container backend (M1)**, the floor on Windows and macOS: each run executes in a throwaway, locked-down Docker container (no network, all capabilities dropped, read-only root filesystem, non-root user, memory/CPU/process caps). It passed its 14-criterion acceptance suite on Windows with Docker Desktop and a check from a live Claude Code session.
>
> Not yet published to PyPI.

| Milestone | What                                                                                         | Status                                                                                                                                                          |
| --------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| M0        | MCP server +`run_code` end-to-end (naive backend, **no isolation**)                  | ✅ done                                                                                                                                                         |
| M1        | Container backend (Docker/Podman) — the cross-platform isolation floor, +`read_workspace` | ✅ accepted 2026-10-14 (all 14 acceptance criteria + live-agent check) — see [`docs/plans/M1/M1-container-backend.md`](docs/plans/M1/M1-container-backend.md) |
| M2        | Native Linux isolation (namespaces + cgroups v2 + seccomp, no root required)                 | ✅ accepted 2026-10-31 (all 17 acceptance criteria + live-agent check) — see [`docs/plans/M2/M2-native-warden.md`](docs/plans/M2/M2-native-warden.md) |
| M3        | Audit log + resource reporting                                                               | planned                                                                                                                                                         |
| M4        | Install/onboarding polish,`SECURITY.md` threat model                                       | planned                                                                                                                                                         |

## One-time setup

Not on PyPI yet, so install from a clone. You need Python ≥ 3.11. On Windows and macOS you also need Docker (Docker Desktop, running Linux containers). On Linux, Docker is optional: native isolation needs `vestibule-mcp setup`, run once, and the prerequisites in [Native isolation on Linux](#native-isolation-on-linux).

```bash
git clone https://github.com/victorperson999/Vestibule.git && cd Vestibule
python -m venv .venv
.venv/bin/pip install .                 # Windows: .venv\Scripts\pip install .
claude mcp add --scope user vestibule -- "$PWD/.venv/bin/vestibule-mcp"
# Windows: claude mcp add --scope user vestibule -- C:\path\to\Vestibule\.venv\Scripts\vestibule-mcp.exe
# (--scope user = available in every project; the default registers it for this folder only)

# Linux: the native sandbox's root filesystems, unpacked from the same pinned images
# (about 123 MB to download, no Docker needed):
.venv/bin/vestibule-mcp setup

# Windows and macOS (on Linux, only for the container fallback): the two sandbox images,
# pinned to exact bytes (Vestibule never pulls images itself):
docker pull docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
docker pull docker.io/library/node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
```

Then ask your agent to run some code. The first `run_code` call checks your setup, and every result states the isolation that actually applied (normally `isolation: native` on Linux and `isolation: container` elsewhere).

- If a call is ever `Blocked:`, the message names the exact fix and says whether to retry or restart.
- `setup` unpacks about 357 MB into `~/.vestibule/rootfs`, checking every byte against the pins; a second run doesn't touch the network. If your MCP config sets `VESTIBULE_ROOTFS_DIR` or `VESTIBULE_IMAGE_*`, set them for `setup` too.
- The images are `python:3.12-slim` and `node:22-slim` as of 2026-10-10, pinned by digest so they can never change silently. The flip side: they get no upstream security fixes until Vestibule re-pins them.
- Switched Docker Desktop's image store (classic ↔ containerd)? Pull again — the two stores are separate.
- Custom images (`VESTIBULE_IMAGE_PYTHON` / `VESTIBULE_IMAGE_NODE`) must provide `bash`, coreutils `timeout` (every run is wrapped in it so a container ends itself at its deadline), and the language's interpreter on `PATH`, and work as an arbitrary non-root user on a read-only root filesystem. Pin them as `name@sha256:…`; an unpinned tag still works but logs a warning.

Dev install and hand-editing the MCP config: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

## Configuration

Set these as environment variables in Vestibule's entry in your MCP client's config ([how](docs/GETTING_STARTED.md#register-the-server-with-an-agent)). They are read once, when the agent starts the server, so restart it after a change. Numbers must be positive whole numbers, and on/off settings take `1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off`; a blank value counts as unset. If any setting is invalid, the server still starts, but it refuses every tool call with a `Blocked:` message that names the setting, so your agent can tell you what to fix. Nothing runs until you fix it and restart the server.

| Variable                        | Default                     | What it does                                                                                                                                                                                                                                                                                                                |
| ------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VESTIBULE_WORKSPACE`         | `~/.vestibule/workspace`  | Host folder mounted at `/workspace`, the one place runs can keep files. Created on first use.                                                                                                                                                                                                                             |
| `VESTIBULE_WORKSPACE_RO`      | off                         | `1`/`true`/`yes`/`on` mounts the workspace read-only.                                                                                                                                                                                                                                                               |
| `VESTIBULE_RUNTIME`           | `auto`                    | `auto` (Docker, then Podman), `docker`, or `podman`.                                                                                                                                                                                                                                                                  |
| `VESTIBULE_BACKEND`           | `auto`                    | `auto` uses native isolation on Linux once `vestibule-mcp setup` has run, and the container backend otherwise (or when it protects more). `native` and `container` force one backend. `naive` runs code directly on your machine with **no isolation** (development only; results say `isolation: none`). |
| `VESTIBULE_IMAGE_PYTHON`      | pinned `python:3.12-slim` | Image for `python` and `bash` runs. See the override rules above.                                                                                                                                                                                                                                                       |
| `VESTIBULE_IMAGE_NODE`        | pinned `node:22-slim`     | Image for `node` runs.                                                                                                                                                                                                                                                                                                    |
| `VESTIBULE_ROOTFS_DIR`        | `~/.vestibule/rootfs`     | Linux only: where `vestibule-mcp setup` unpacks the native sandbox's root filesystems, and where the server looks for them. Keep it on the Linux disk and outside the workspace.                                                                                                                                         |
| `VESTIBULE_DEFAULT_TIMEOUT_S` | `10`                      | Timeout when a call doesn't set `timeout_seconds`.                                                                                                                                                                                                                                                                        |
| `VESTIBULE_MAX_TIMEOUT_S`     | `60`                      | Ceiling on `timeout_seconds`.                                                                                                                                                                                                                                                                                             |
| `VESTIBULE_MAX_CODE_BYTES`    | `262144`                  | Larger code is refused before anything runs.                                                                                                                                                                                                                                                                                |
| `VESTIBULE_MAX_OUTPUT_BYTES`  | `20000`                   | Output returned per stream. Longer output is truncated, and a run printing more than twice this is stopped.                                                                                                                                                                                                                 |
| `VESTIBULE_MEM_MB`            | `256`                     | Memory cap, with no swap on top.                                                                                                                                                                                                                                                                                            |
| `VESTIBULE_CPU_PCT`           | `75`                      | CPU cap, as a percentage of one core.                                                                                                                                                                                                                                                                                       |
| `VESTIBULE_PIDS_MAX`          | `128`                     | Cap on processes and threads inside a run.                                                                                                                                                                                                                                                                                  |
| `VESTIBULE_TMPFS_MB`          | `64`                      | Size of the run's `/tmp`, which is also its `$HOME`.                                                                                                                                                                                                                                                                    |
| `VESTIBULE_MAX_CONCURRENT`    | `4`                       | Runs at once. A further call waits up to 5 s for a slot, then is `Blocked:`.                                                                                                                                                                                                                                              |

## Native isolation on Linux

On Linux, Vestibule doesn't need Docker. Each run gets a fresh sandbox built from the kernel's own features, without root:

- **Its own world:** new user, PID, mount, network, UTS, IPC and cgroup namespaces. The guest runs as the server's own uid and gid, so as you, not root (unless you start the server as root, which Vestibule never needs), with every capability dropped and `no_new_privs` set.
- **A filesystem jail:** `pivot_root` into a read-only copy of the same pinned images the container backend uses, plus `/workspace`, a size-capped `/tmp`, and a read-only `/sandbox` holding the script. None of your other files exist in there.
- **No network:** the network namespace has only `lo`. `AF_VSOCK`, which a network namespace doesn't isolate, is refused by the syscall filter.
- **Resource limits:** each run gets its own systemd scope with cgroup v2 limits on memory (no swap), CPU and processes, the same values as the container backend.
- **A syscall allowlist** (seccomp), derived from Docker's default profile and compiled by the system's libseccomp. A named set of dangerous calls (`mount`, `unshare`, `bpf`, `keyctl`, `reboot`, `socket(AF_VSOCK)` and others) fails with `EPERM` and is listed in the result, for example `blocked syscalls: mount`. After 256 such reports the result says reporting stopped, and further ones fail with `ENOSYS`. Any other call outside the allowlist fails without a report, with `EPERM` (or `ENOSYS` for `clone3`, as in Docker, so programs fall back to `clone`).
- **Debuggers work:** as in Docker's profile, `ptrace` is allowed, so a debugger or tracer works on processes inside the same sandbox.

**What `auto` picks.** On Linux, the strongest guarantee that verifies, in this order: `native`, then a full `container` (if Docker or Podman works), then `native-degraded`, then `container-degraded`, then `Blocked:` with both reasons. A full container beats degraded native because limits protect the host itself: without a memory cap, a native sandbox could set off the *host's* OOM killer. On Windows and macOS, `auto` uses the container backend and never tries native.

**Speed,** measured through the server itself, 50 `print(1)` runs per backend after a warm-up, on WSL2 Ubuntu 26.04 (12 CPUs) with Docker Desktop 28.5.1:

| Backend   | Median | p95    |
| --------- | ------ | ------ |
| native    | 82 ms  | 107 ms |
| container | 392 ms | 413 ms |

The container path here goes through Docker Desktop's VM, a hop a Docker Engine user on Linux doesn't pay, and each container run also starts one cleanup check (`docker ps`). So these are this host's numbers, not a speed-up factor. `scripts/bench_coldstart.py` reproduces them.

### Where native works

| Linux                                           | Prerequisites as shipped                                                                                                     | Verdict                                                                     |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| WSL2, Ubuntu 26.04, systemd enabled             | All met (Microsoft's kernel 6.6, no AppArmor)                                                                                | `native`, **measured**                                                      |
| Ubuntu 23.10 and later (24.04 LTS too), stock   | AppArmor restricts unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`); lifting that needs root | the container backend if Docker or Podman works, otherwise `Blocked:`; expected |
| Debian 12 and later                             | kernel 6.1+, systemd 252+                                                                                                    | `native`, expected                                                          |
| Fedora 38 and later                             | kernel 6.2+, systemd 253+                                                                                                    | `native`, expected                                                          |
| Arch                                            | current kernel and systemd                                                                                                   | `native`, expected                                                          |
| arm64, any distro                               | supported in the code, never run on a real arm64 host                                                                        | **untested**                                                                |

The prerequisites, which the first `run_code` call checks for you:

- Linux kernel 5.9 or later (`close_range`; pidfds need 5.3).
- cgroup v2.
- A systemd user manager that delegates `cpu`, `memory` and `pids`. systemd 252 and later do by default. Older versions delegate only `memory` and `pids`, so runs report `native-degraded (limits not applied: cpu)`.
- `busctl`, part of systemd.
- `libseccomp.so.2`, a systemd dependency on these distros (`apt install libseccomp2` otherwise).
- Unprivileged user namespaces, not restricted.

"Expected" rows follow from those prerequisites, not from a run. M4's fresh-machine test runs on a stock Ubuntu 24.04 VM.

## Know the limits

- **Not a VM.** On Linux the sandbox shares your host's kernel, so a kernel exploit could in principle escape it. On Windows and macOS, Docker Desktop runs it inside its own Linux VM. The full threat model ships as `SECURITY.md` in M4.
- **The workspace is writable, and its size is not capped.** Code can create, change, and delete anything in `/workspace`; that is what it's for. The root filesystem is read-only and `/tmp` is capped, but nothing limits how much a run writes to the workspace. `VESTIBULE_WORKSPACE_RO=1` makes it read-only.
- **Treat the workspace like an untrusted download.** Sandboxed code can leave symlinks in it, and on your machine they are real symlinks: any tool that follows one can read or write outside the workspace (picture `report.txt` pointing at `~/.ssh/id_ed25519`). Vestibule's own `read_workspace` refuses symlinks, and the `run_code` description tells the agent to use it. Never point `VESTIBULE_WORKSPACE` at a folder other tools use, and don't give your agent's own file tools blanket access to it.
- **With the container backend, a deliberately hostile run can outlive its deadline.** Every container ends itself at its deadline even if the server is killed, but code written to stop that timer (it runs as the same user) can keep its container alive until a later Vestibule run removes it. Meanwhile it stays inside the caps its result reported: all of them for `isolation: container`, only the ones still applied for `container-degraded`. (A native sandbox is stopped from outside its PID namespace, by processes the guest can't see.)
- **Forced native without a user manager has no memory cap.** With `VESTIBULE_BACKEND=native` on a host without a systemd user manager that delegates the controllers, runs report `native-degraded (limits not applied: …)`, and a memory bomb can set off your host's OOM killer. Before Linux 5.14 nothing caps a fork bomb there either. That is why `auto` prefers a full container over degraded native.
- **Native sandboxes run outside your agent's own cgroup,** in your user manager's `app.slice`. A memory limit you put on the agent app doesn't count them; each sandbox has its own limits instead.
- **A native guest can still learn some things about your machine**, as it could in a container: your numeric uid and gid, the kernel version, and the CPU and memory information in `/proc`. The mount table (`/proc/self/mountinfo`) shows the host paths of the rootfs and the workspace, so your username, much as Docker's does for its bind mounts. And your supplementary groups still count for permission checks (no unprivileged process can drop them), though nothing they unlock is visible inside the jail.
- **Podman is experimental.** It is used only if it passes the setup check, and only Docker is tested.

## How it works (design)

Two halves: the **MCP server** decides *whether/what* (validation, limits, schemas, audit), and a **warden** backend decides *how* to isolate and run. Every result carries an `isolation:` field stating what actually protected the run — `native` and `container`, `native-degraded` and `container-degraded` (isolated, but some resource limits could not be applied or verified; the result names them and why), and `none` — and the tool never claims protection that wasn't applied.

Honest scope, stated up front: this is namespace/container isolation sharing the host kernel, **not** a hardened VM boundary. The goal is to make the common, realistic agent risks — prompt-injected exfiltration, destructive commands, resource exhaustion — structurally impossible, not to defend against kernel 0-days. The full threat model ships as `SECURITY.md` in M4.

## Documentation

| File                                                                              | What's in it                                                                               |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)                                   | Deep technical design: the warden lifecycle, layered isolation, MCP server design.         |
| [`docs/PLAN.md`](docs/PLAN.md)                                                   | Roadmap: milestones 0–4 with acceptance criteria.                                         |
| [`docs/plans/M1/M1-container-backend.md`](docs/plans/M1/M1-container-backend.md) | The detailed M1 contract (container profile,`read_workspace` jail, acceptance criteria). |
| [`docs/plans/`](docs/plans/)                                                     | Per-milestone folders of step plans, each with its decisions and review findings.          |
| [`docs/HISTORY.md`](docs/HISTORY.md)                                             | The build story in plain language: every decision, sign-off, and finished step.            |
| [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md)                                         | Regression index: per step, its commits, files, guarding tests, and known open issues.     |
| [`docs/reviews/`](docs/reviews/)                                                 | Pre-implementation adversarial design reviews.                                             |
| [`CLAUDE.md`](CLAUDE.md)                                                         | Invariants and operating rules for AI-assisted development of this repo.                   |
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md)                             | Developer guide: dev install, registration, running the checks, troubleshooting.           |

## License

[MIT](LICENSE)
