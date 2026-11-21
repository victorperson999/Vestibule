# Vestibule

**Run untrusted AI-agent code in a sandbox on your own machine: free, and with no hosted sandbox service in the loop.**

A local, kernel-isolated code-execution sandbox for AI agents, exposed as an MCP server.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/victorperson999/Vestibule/blob/main/LICENSE) [![Python ≥ 3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](https://www.python.org/downloads/) [![MCP Server](https://img.shields.io/badge/MCP-Server-6E56CF?logo=modelcontextprotocol&logoColor=white)](https://modelcontextprotocol.io) [![Status: Early Development](https://img.shields.io/badge/status-early%20development-orange.svg)](https://github.com/victorperson999/Vestibule#status) [![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/victorperson999/badge.svg)](https://hub.xerj.org/contributors/victorperson999)

![Terminal recording. A short program runs twice. On the machine itself it reads a fake key file and connects to 1.1.1.1. Run through Vestibule, the same program reports "No such file or directory" for the file and "Network is unreachable" for the connection, and the result ends with "isolation: native".](https://raw.githubusercontent.com/victorperson999/Vestibule/main/docs/demo-2.gif)

*A real Claude Code session in Ubuntu under WSL2, recorded on 2026-11-11 with a development install of 0.1.1's code and the flags of the stricter line from [What is and isn't sandboxed](https://github.com/victorperson999/Vestibule#what-is-and-isnt-sandboxed), so `run_code` was the agent's only way to run code. A fake key is planted on the machine, outside the workspace. The same short program runs first on the machine itself and then through Vestibule: it tries to read the key and to open a connection to 1.1.1.1, and prints one line for each. It sends no data. Claude Code folds tool results, so the agent was asked to repeat the result, and its copy matches the tool's result exactly. Edits: Claude Code's start-up banner and the pasting of the prompt are cut, the end of the session after the result is dropped, and the last frame is held. `native` needs a Linux that allows unprivileged user namespaces, as WSL2 does. An install on Windows itself with Docker Desktop, or on stock Ubuntu 24.04 with Docker Engine from Docker's apt repository, gets `isolation: container`, and macOS is expected to.*

> A *vestibule* is the small sealed entry chamber you pass through between outside and inside. Vestibule sits between an AI agent and your host machine: the code an agent sends to Vestibule runs *inside* the chamber, not on your real system. Its one opening is a workspace folder.

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

Only code sent to `run_code` goes through the chamber: see [What is and isn't sandboxed](https://github.com/victorperson999/Vestibule#what-is-and-isnt-sandboxed).

---

## Why use Vestibule?

**The problem:** Agents no longer just suggest code — they write it and execute it, autonomously, in a loop. Running model-generated code on your real machine is the dangerous part: a prompt-injected instruction can exfiltrate secrets (`~/.ssh`, `~/.aws`, `.env`), a hallucinated argument can delete the wrong directory, a runaway loop can eat the machine. That's the *what*: untrusted agent code needs somewhere safe to run.

**The answer:** Safe execution is already a product category — hosted sandboxes like E2B and Daytona solve it well, but on their terms: you pay per usage, every execution adds a network round-trip to the agent loop, and the service adds one more third party holding your code and data.

**Vestibule's reason to exist** is refusing all three of those costs at once:

- **Local** — execution is a process spawn, not an HTTP call. No round-trip in the agent loop. The sandbox needs no network after `setup`.
- **Free** — open source (MIT), no account, no metering, no per-second bill deciding how often your agent gets to run code.
- **No sandbox vendor** — Vestibule itself sends nothing anywhere: no account, no telemetry. The only downloads are `setup`'s, from Docker Hub. (Your agent still sends its code, and whatever a run prints, to its model's provider, as it would anyway.)

**What it is:** an open-source [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any AI coding agent — Claude Code, Claude Desktop, Cursor, or anything that speaks MCP — a `run_code` tool. The tool executes the model's generated code inside an isolated sandbox instead of on your actual machine: no network access by default, resource limits, a filesystem the guest can't see out of, and honest reporting of what protection was actually applied. The isolation underneath is real kernel-level machinery — Linux namespaces, cgroups v2, seccomp, the same mechanisms rootless containers are built on — not a best-effort wrapper. The code can be Python 3.12, Node 22 or Bash, and a second tool, `read_workspace`, reads the files a run leaves in the workspace.

> ⚠️ **Early development — do not rely on this for hostile code yet.** [Status](https://github.com/victorperson999/Vestibule#status) says what has been tested, and [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md) says what it protects and what it doesn't.

## What is and isn't sandboxed

- **Only code sent to `run_code` is sandboxed.** Your agent's own shell and file tools still run on your machine, and an agent that has them can use them instead.
- **A sandbox-only session** makes `run_code` the only way the agent can run code. With Claude Code:

  ```bash
  claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
  ```

  ```powershell
  # Windows PowerShell drops an empty "" argument, so go through cmd:
  cmd /c --% claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
  ```
- **What that line doesn't cover:** the other MCP servers you've added still run on your machine, your Claude account's connectors still reach their services, and your plugins still load. `claude mcp list` shows the servers. In our session the agent called no tool but `run_code`, and one plugin's hook still ran a script on the machine after every turn.
- **To leave the other servers and connectors out,** save the JSON from [Connect a client](https://github.com/victorperson999/Vestibule#connect-a-client) as `only-vestibule.json` and add three flags. `--permission-mode dontAsk` refuses a tool that isn't allowed instead of asking you. In a session on Windows this left the agent no tool that acts on your machine except Vestibule's two (Claude Code keeps a few of its own, such as `EndConversation`). A plugin's own MCP server goes too, but its hooks don't: that plugin's hook ran in this session as well. Disable a plugin (`/plugin`) if you don't want its hooks running.

  ```bash
  claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace --strict-mcp-config --mcp-config only-vestibule.json --permission-mode dontAsk
  ```

  ```powershell
  cmd /c --% claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace --strict-mcp-config --mcp-config only-vestibule.json --permission-mode dontAsk
  ```
- **Other clients** have their own settings for this. Only Claude Code's flags were tried.

## Quick start

You need an MCP client (these steps use [Claude Code](https://claude.com/claude-code)) and [uv](https://docs.astral.sh/uv/), which installs Vestibule in its own environment. Each platform's steps install uv with uv's own installer ([other ways](https://docs.astral.sh/uv/getting-started/installation/)), and **you need a new terminal after that** so `uv` is found.

| Your machine | What you need first | The isolation you'll get |
| --- | --- | --- |
| Windows | Docker Desktop, running | `container`, measured |
| Ubuntu 23.10 and later, on a machine or in a VM | `curl`, and Docker Engine from Docker's apt repository | `container`, measured on 24.04 |
| Linux with systemd: Debian, Fedora, Arch, WSL2 | `curl`; on a minimal Debian, two more packages and a reboot | `native`: measured on WSL2 (Ubuntu 26.04) and Debian 13; expected on Debian 12, Fedora 38 and later, and Arch; untested on arm64 |
| macOS | Docker Desktop for Mac, running | `container`, expected: no sandboxed run has been made on a Mac |

What was measured where, and what native needs: [Where native works](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#where-native-works).

### Windows

1. **Install Docker Desktop and start it,** as [Docker's page for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) shows.
   - If its recommended install stops and asks for the Virtual Machine Platform feature, this fixed it on our one test machine: `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` in an admin PowerShell, then a restart.
2. **Install uv, then open a new terminal:**

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. **Install Vestibule and run its setup:**

   ```powershell
   uv tool install vestibule-mcp
   uv tool update-shell          # if it says it changed your PATH, open a new terminal
   vestibule-mcp setup
   ```

   `setup` prints `ready in this terminal's environment: isolation container`.
4. **Connect Claude Code** (other clients: [Connect a client](https://github.com/victorperson999/Vestibule#connect-a-client)):

   ```powershell
   claude mcp add --scope user vestibule -- vestibule-mcp
   ```
5. **Check it:** ask your agent to run `print(1)` with Vestibule's `run_code` tool. The result ends with `isolation: container`. [Check it works](https://github.com/victorperson999/Vestibule#check-it-works) shows a whole result, and how to see it if your agent doesn't.

**For native isolation on Windows,** run your agent and Vestibule both inside WSL2 with systemd on, and follow the Linux steps there. A Windows client driving a server inside WSL isn't designed or tested.

### Ubuntu 23.10 and later

This is for Ubuntu installed on a machine or in a VM. Ubuntu under WSL2 doesn't have the restriction the next paragraph describes: follow the Linux section below.

Ubuntu restricts the kernel feature that native isolation needs (unprivileged user namespaces, through AppArmor), and lifting that needs root. So Vestibule uses its container backend there, and you need Docker.

1. **Install `curl` and Docker Engine:**
   - `sudo apt install curl`, for uv's installer.
   - Docker Engine from Docker's own apt repository, as [Docker's page for Ubuntu](https://docs.docker.com/engine/install/ubuntu/) shows. Then `sudo usermod -aG docker $USER`, and log out and back in.
   - **Not the snap** (`snap install docker`): it can't run Vestibule, because its containers don't start with `no-new-privileges`, which Vestibule always sets.
   - **Know what the `docker` group means:** its members can become root on that machine. Rootless Docker avoids that, and hasn't been tried with Vestibule.
2. **Install uv, then open a new terminal:**

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. **Install Vestibule and run its setup:**

   ```bash
   uv tool install vestibule-mcp
   vestibule-mcp setup
   ```

   `setup` says why native isn't used, and prints `ready in this terminal's environment: isolation container`.
4. **Connect Claude Code** (other clients: [Connect a client](https://github.com/victorperson999/Vestibule#connect-a-client)):

   ```bash
   claude mcp add --scope user vestibule -- vestibule-mcp
   ```
5. **Check it:** ask your agent to run `print(1)` with Vestibule's `run_code` tool. The result ends with `isolation: container`. [Check it works](https://github.com/victorperson999/Vestibule#check-it-works) shows a whole result, and how to see it if your agent doesn't.

### Linux with systemd (Debian, Fedora, Arch, WSL2)

1. **Install `curl`,** for uv's installer (`sudo apt install curl` on Debian and Ubuntu). On a minimal Debian, install three packages and reboot, or `setup` can't download and runs lose their limits. If that Debian has no `sudo`, become root with `su -` and type both lines without it:

   ```bash
   sudo apt install ca-certificates curl dbus-user-session
   sudo reboot
   ```
2. **Install uv, then open a new terminal:**

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. **Install Vestibule and run its setup:**

   ```bash
   uv tool install vestibule-mcp
   vestibule-mcp setup
   ```

   `setup` prints `ready in this terminal's environment: isolation native`. No Docker is needed.
4. **Connect Claude Code** (other clients: [Connect a client](https://github.com/victorperson999/Vestibule#connect-a-client)):

   ```bash
   claude mcp add --scope user vestibule -- vestibule-mcp
   ```
5. **Check it:** ask your agent to run `print(1)` with Vestibule's `run_code` tool. The result ends with `isolation: native`. [Check it works](https://github.com/victorperson999/Vestibule#check-it-works) shows a whole result, and how to see it if your agent doesn't.

### macOS

**Untested for running code.** On a macOS runner the server starts, answers and shuts down, and the tests that need no Docker pass (a manual check, 2026-11-09). No sandboxed run has been made on a Mac: that runner has no Docker. The steps would be the Linux ones with Docker Desktop for Mac running first, and the expected result is `isolation: container`.

## Check it works

Ask your agent to run `print(1)` with Vestibule's `run_code` tool. With native isolation, the result reads:

```text
exit_code: 0
stdout:
1

usage: wall=120ms, cpu=37ms, mem_peak=13MiB, pids_peak=2 (the whole sandbox, including its setup and file cache)
isolation: native
```

With the container backend:

```text
exit_code: 0
stdout:
1

usage: wall=719ms; cpu, mem_peak and pids_peak not measured: not readable from outside a container
isolation: container
```

*Both from vestibule-mcp 0.1.2 installed from PyPI, on 2026-11-20: the first in Ubuntu 26.04 under WSL2, the second on Windows 11 with Docker Desktop (Docker Engine 28.5.1). Your times will differ.*

**Your agent may not show you this.** Claude Code folds tool results, and an agent may sum one up ("output 1, exit code 0") instead of quoting it. The audit log doesn't depend on the agent: in a terminal, `vestibule-mcp audit` prints a line for each `run_code` call that reached Vestibule, starting with its time, and for a run, the isolation its result reported (if your MCP config sets `VESTIBULE_AUDIT_DIR`, set it in that terminal too):

```text
2026-11-20T23:03:35.569Z  python  ran        isolation=native exit=0 wall=120ms cpu=37ms mem_peak=13MiB  print(1)
```

Check that the line's time is your call's. A call refused with `Blocked:` shows its reason instead of an isolation, and two kinds of call leave no line at all ([which](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#usage-numbers-and-the-audit-log)). Asking your agent to repeat the result word for word works too, but that copy is typed by the model.

| `isolation:` | What it means |
| --- | --- |
| `native` | Linux's own sandbox: namespaces, a filesystem jail and a seccomp filter, with every resource limit applied. |
| `container` | A locked-down container: the whole profile was handed to the container runtime (Docker, or the experimental Podman) to apply. What Vestibule can't check on your machine is in [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md). |
| `native-degraded (…)`, `container-degraded (…)` | Isolated, but some resource limits could not be applied or verified. The parentheses name them, and why. |
| `none` | No isolation is claimed. With `VESTIBULE_BACKEND=naive`, which is for development only, code runs directly on your machine. Otherwise the parentheses say why: `nothing was executed` means the code didn't run, and any other reason, such as `hard controls not confirmed` or `the sandbox did not report back`, means it may have run with no protection claimed. |
| No `isolation:` line, and `Blocked:` instead | Nothing ran. The message names the fix and, where it matters, whether to retry or restart. |

## Connect a client

| Client | Config | Tested? |
| --- | --- | --- |
| Claude Code | `claude mcp add --scope user vestibule -- vestibule-mcp` (`--scope user` makes it available in every project) | tested on Linux (WSL2) and Windows |
| Cursor | `~/.cursor/mcp.json`, the JSON below | tested on Windows |
| Claude Desktop | `claude_desktop_config.json`, the JSON below. On Windows it is in `%APPDATA%\Claude`; version 4.23118.1 has no button for it in Settings | tested on Windows |
| Cursor or Claude Desktop on macOS | the second JSON below (Claude Desktop's file is in `~/Library/Application Support/Claude`) | untested |

```json
{
  "mcpServers": {
    "vestibule": { "command": "vestibule-mcp" }
  }
}
```

On macOS a GUI client doesn't see your terminal's `PATH`, so give it the program's full path (`uv tool dir --bin` prints its folder) and a `PATH` that holds Docker's folder:

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

- **If the file already has an `mcpServers` block,** add the `vestibule` entry inside it.
- **Start the client after `uv tool update-shell`,** and restart it fully after editing its config. A client that was already running through either wasn't tested.
- **A client that can't find `docker`** takes an `env` entry with a `PATH`, as in the second block.
- **Settings** go in the same `env` entry: see [Configuration](https://github.com/victorperson999/Vestibule#configuration).

## First-run troubleshooting

- **`uv` or `vestibule-mcp` isn't found:** open a new terminal. On Windows, run `uv tool update-shell` first.
- **`setup` fails with `CERTIFICATE_VERIFY_FAILED`:** install `ca-certificates` (a minimal Debian doesn't have it).
- **`isolation: native-degraded (… no systemd user bus)`:** install `dbus-user-session` and reboot.
- **`Blocked:` saying Ubuntu restricts unprivileged user namespaces, or naming the snap:** see [Ubuntu 23.10 and later](https://github.com/victorperson999/Vestibule#ubuntu-2310-and-later).
- **`daemon not reachable`:** start Docker. If the message says this user may not open docker's socket, you aren't in the `docker` group yet: run `sudo usermod -aG docker $USER`, log out and back in, then restart your agent.
- **A warning about `server/discover` with 31 validation errors,** once per session in the server's log: harmless. Claude Code first sends a request this version of the MCP SDK doesn't know, then falls back, and the session works.
- **Anything else:** every `Blocked:` message names its fix, and [`docs/GETTING_STARTED.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md#troubleshooting) has the longer list.

## What it protects, and what it doesn't

Four things that change how you use it. The full account, with what each claim rests on, is [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md).

- **Not a VM.** On Linux the sandbox shares your host's kernel, so a kernel exploit could in principle escape it. On Windows and macOS, Docker Desktop runs it inside its own Linux VM.
- **The workspace is writable, and its size is not capped.** Code can create, change, and delete anything in `/workspace`; that is what it's for. The root filesystem is read-only and `/tmp` is capped, but nothing limits how much a run writes to the workspace. `VESTIBULE_WORKSPACE_RO=1` makes it read-only.
- **Treat the workspace like an untrusted download.** Sandboxed code can leave symlinks in it, and on your machine they are real symlinks: any tool that follows one can read or write outside the workspace (picture `report.txt` pointing at `~/.ssh/id_ed25519`). Vestibule's own `read_workspace` refuses symlinks, and the `run_code` description tells the agent to use it. Never point `VESTIBULE_WORKSPACE` at a folder other tools use, and don't give your agent's own file tools blanket access to it.
- **A run's output goes back to your agent.** Whatever the code prints, the agent reads, and so does its model's provider. The sandbox keeps your other files out of the code's reach; it doesn't judge what the code prints about the files it can reach.

## Configuration

Every setting, with its default and its rules, is in the reference's [Configuration](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#configuration).

Set them as environment variables in Vestibule's entry in your MCP client's config ([how](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md#register-the-server-with-an-agent)). They are read once, when the agent starts the server, so restart it after a change. If any setting is invalid, the server still starts, but it refuses every tool call with a `Blocked:` message that names the setting, so your agent can tell you what to fix.

The ones you're most likely to want: `VESTIBULE_WORKSPACE` (the host folder mounted at `/workspace`), `VESTIBULE_WORKSPACE_RO` (mount it read-only), `VESTIBULE_BACKEND` (choose the backend), `VESTIBULE_MEM_MB` and `VESTIBULE_CPU_PCT` (the memory and CPU caps), and `VESTIBULE_DEFAULT_TIMEOUT_S` (the timeout when a call doesn't set one).

## What `setup` does

The details, the rules for a custom image among them: [What `setup` does, in detail](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#what-setup-does-in-detail).

`vestibule-mcp setup` gets what this machine can use (about 123 MB either way: on Linux the native sandbox's root filesystems, needing no Docker; otherwise the two sandbox images, through Docker). It ends with the same check a first call makes, and prints `ready in this terminal's environment: isolation X` or the exact `Blocked:` fix.

- If a call is ever `Blocked:`, the message names the exact fix and says whether to retry or restart.
- To see what your agents ran: `vestibule-mcp audit` prints the last 20 calls, one line each, with the time, language, outcome, isolation, exit code, usage, any denied syscalls and the code's first line. `audit 50` prints the last 50. Everything taken from a record is printed escaped, so code an agent ran can't drive your terminal. What the records keep, and who can read them: [Usage numbers and the audit log](https://github.com/victorperson999/Vestibule#usage-numbers-and-the-audit-log).
- Run Vestibule as your own user. It never needs root, and on Linux and macOS a server started as root refuses to run code, because the code would run as root too.
- Where native is used, `setup` unpacks about 357 MB into `~/.vestibule/rootfs`, checking every byte against the pins; a second run doesn't touch the network.
- The images are `python:3.12-slim` and `node:22-slim` as of 2026-11-09, pinned by digest so they can never change silently. The flip side: they get no upstream security fixes until Vestibule re-pins them.

The same two images by hand, pinned to exact bytes (a tool call never pulls anything):

```bash
docker pull docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
docker pull docker.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
```

Working on Vestibule itself (a clone, a dev install, the checks): [`docs/GETTING_STARTED.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md).

## Usage numbers and the audit log

The full account: [Usage numbers and the audit log](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md#usage-numbers-and-the-audit-log) in the reference.

- **A run's result says what it used,** on a `usage:` line. Under `native`, that's `wall`, `cpu`, `mem_peak` and `pids_peak`, read from the run's own cgroup; under `container`, `wall` only, and the line says why.
- **Every `run_code` call that reaches Vestibule leaves records** in `~/.vestibule/audit/` (`VESTIBULE_AUDIT_DIR`). Two kinds of call leave none; the full account says which. `vestibule-mcp audit` prints the last 20 calls, `audit 50` the last 50.
- **What a record keeps:** the code, cut at 4 KiB, with its outcome, isolation and usage numbers. Of the output it keeps only the sizes, never the content.
- **Who can read it:** on Linux and macOS, each file is 0600, and a folder Vestibule creates is 0700. On Windows, the default folder under your profile is limited to you, SYSTEM and Administrators, but a custom one may let other users read it.
- **A failed write never stops a run.** The result says so on an `audit:` line.
- **It isn't tamper-proof.** The sandboxed code can never reach it, but you, and any tool running as you, can edit it.
- **Nothing deletes old records.** 100 MB holds about 20,000 runs.

## Uninstall

**This deletes your workspace and your audit log:** both live in `~/.vestibule`. Copy out what you want first, and quit your MCP client, because a running server holds files open on Windows.

```bash
claude mcp remove --scope user vestibule      # GUI clients: delete the entry from the config
uv tool uninstall vestibule-mcp
chmod -R u+rwX ~/.vestibule && rm -rf ~/.vestibule   # sandboxed code can lock its own folders
# only if setup pulled the images:
docker image rm docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b docker.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
```

On Windows, the third line is:

```powershell
Remove-Item -Recurse -Force $HOME\.vestibule
```

**What stays:**
- uv, and any Python it downloaded;
- uv's package cache, which holds a copy of Vestibule and its dependencies (`uv cache clean` empties it);
- your MCP client's own logs of the server (Claude Code keeps `mcp-logs-vestibule` folders in its cache, one for each folder you used it in);
- a folder you set with `VESTIBULE_WORKSPACE`, `VESTIBULE_ROOTFS_DIR` or `VESTIBULE_AUDIT_DIR`;
- anything named `vestibule-*` in the temp folder after a hard kill (root-owned ones in `/tmp` need `sudo`);
- any container that `docker ps -a --filter label=vestibule.run=1` lists.

## How it works (design)

Two halves: the **MCP server** decides *whether/what* (validation, limits, schemas, audit), and a **warden** backend decides *how* to isolate and run. Every result of a run carries an `isolation:` field stating what actually protected it — `native` and `container`, `native-degraded` and `container-degraded` (isolated, but some resource limits could not be applied or verified; the result names them and why), and `none`. The tool claims only protection it applied or handed to the container runtime to apply, and never one it found missing. What the container backend can't check on your machine is in [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md), §3.14. A `Blocked:` reply ran nothing, and has no such line.

Honest scope, stated up front: this is namespace/container isolation sharing the host kernel, **not** a hardened VM boundary. For a run that reports `native` or `container`, your files outside the workspace, the network, and the machine's memory, CPU and processes beyond the run's caps are out of the run's reach, short of a bug in the kernel or the container runtime. What stays possible, and everything else, is in [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md).

## Status

Two backends are accepted, and every result states the isolation that actually applied.

- **Native isolation on Linux (M2):** each run executes in a fresh sandbox built from the kernel's own namespaces, cgroup limits and a seccomp allowlist, with no root and no Docker. It passed its 17-criterion acceptance on WSL2 Ubuntu 26.04 and a check from a live Linux Claude Code session.
- **The container backend (M1)**, the floor on Windows and macOS: each run executes in a throwaway, locked-down Docker container (no network, all capabilities dropped, read-only root filesystem, non-root user, memory/CPU/process caps). It passed its 14-criterion acceptance suite on Windows with Docker Desktop and a check from a live Claude Code session.
- **Usage numbers and an audit log (M3):** every result reports what the run used, measured from outside the sandbox, and every call leaves a record in a local audit log that `vestibule-mcp audit` prints. It passed its 10-criterion acceptance, including live Claude Code sessions on Linux (native) and Windows (container).
- **Install and first run (M4):** released on PyPI as `vestibule-mcp`. On fresh VMs the quick start's commands alone ended in the expected isolation on Debian, on Ubuntu with Docker and on Windows. The first run took under two minutes on one of the three (Ubuntu 117.7 s, Debian 150.5 s, Windows not timed), and the project's owner made the runs, not a stranger.

| Milestone | What | Status |
| --- | --- | --- |
| M0 | MCP server + `run_code` end-to-end (naive backend, **no isolation**) | ✅ done |
| M1 | Container backend (Docker/Podman) — the cross-platform isolation floor, + `read_workspace` | ✅ accepted 2026-10-14 (all 14 acceptance criteria + live-agent check) — see [`docs/plans/M1/M1-container-backend.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M1/M1-container-backend.md) |
| M2 | Native Linux isolation (namespaces + cgroups v2 + seccomp, no root required) | ✅ accepted 2026-10-31 (all 17 acceptance criteria + live-agent check) — see [`docs/plans/M2/M2-native-warden.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M2/M2-native-warden.md) |
| M3 | Audit log + resource reporting | ✅ accepted 2026-11-07 (all 10 acceptance criteria + live-agent checks) — see [`docs/plans/M3/M3-audit-usage.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M3/M3-audit-usage.md) |
| M4 | Install and first-run polish, [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md) | ✅ accepted 2026-11-11, as amended (on PyPI; the two-minute first run was met on one test machine of three) — see [`docs/plans/M4/M4-adoption.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M4/M4-adoption.md) |

## Documentation

| File | What's in it |
| --- | --- |
| [`SECURITY.md`](https://github.com/victorperson999/Vestibule/blob/main/SECURITY.md) | What Vestibule protects and what it doesn't, each claim with its source, and how to report a vulnerability. |
| [`docs/REFERENCE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/REFERENCE.md) | The details behind this page: every setting, what `setup` does, native isolation on Linux and where it works, and the usage numbers and the audit log. |
| [`docs/ARCHITECTURE.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/ARCHITECTURE.md) | Deep technical design: the warden lifecycle, layered isolation, MCP server design. |
| [`docs/PLAN.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/PLAN.md) | Roadmap: milestones 0–4 with acceptance criteria. |
| [`docs/plans/M1/M1-container-backend.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/plans/M1/M1-container-backend.md) | The detailed M1 contract (container profile, `read_workspace` jail, acceptance criteria). |
| [`docs/plans/`](https://github.com/victorperson999/Vestibule/tree/main/docs/plans) | Per-milestone folders of step plans, each with its decisions and review findings. |
| [`docs/HISTORY.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/HISTORY.md) | The build story in plain language: every decision, sign-off, and finished step. |
| [`docs/BUILD_LOG.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/BUILD_LOG.md) | Regression index: per step, its commits, files, guarding tests, and known open issues. |
| [`docs/reviews/`](https://github.com/victorperson999/Vestibule/tree/main/docs/reviews) | Pre-implementation adversarial design reviews. |
| [`CLAUDE.md`](https://github.com/victorperson999/Vestibule/blob/main/CLAUDE.md) | Invariants and operating rules for AI-assisted development of this repo. |
| [`docs/GETTING_STARTED.md`](https://github.com/victorperson999/Vestibule/blob/main/docs/GETTING_STARTED.md) | Developer guide: dev install, registration, running the checks, troubleshooting. |

## License

[MIT](https://github.com/victorperson999/Vestibule/blob/main/LICENSE)
