# Vestibule — Architecture

The technical design. Two halves: the **MCP server** (front desk — decides *whether/what*) and the **warden** (isolation core — decides *how*). This document specifies both.

---

## System overview

```
┌─────────────────┐   MCP (stdio / JSON-RPC)  ┌──────────────────────┐
│  AI agent host  │ ───────────────────────▶ │   MCP server (Python) │
│ (Claude Code /  │ ◀─────────────────────── │   — tool schemas       │
│  Cursor / etc.) │    tool results + logs    │   — validation/policy  │
└─────────────────┘                           │   — audit logger       │
                                               └──────────┬───────────┘
                                                          │ async subprocess, one per run;
                                                          │ systemd scope + cgroup limits first
                                                          ▼
                                         ┌─────────────────────────────────┐
                                         │  warden helper, one per run     │
                                         │  (Python + ctypes, no shell)    │
                                         │                                 │
                                         │  unshare(NEWUSER|NEWPID)        │
                                         │  identity uid/gid map → fork()  │
                                         │  relays status; answers seccomp │
                                         │  notifications with EPERM       │
                                         └────────────────┬────────────────┘
                                                          ▼
                                         ┌─────────────────────────────────┐
                                         │  init = PID 1 of the sandbox    │
                                         │                                 │
                                         │  unshare(NEWNS|NEWNET|NEWUTS|   │
                                         │          NEWIPC|NEWCGROUP)      │
                                         │  mounts → pivot_root            │
                                         │  drop caps → no_new_privs       │
                                         │  → seccomp → fork the guest     │
                                         └────────────────┬────────────────┘
                                                          ▼
                                                 ┌─────────────────┐
                                                 │  Guest process  │
                                                 │ (agent's code)  │
                                                 └─────────────────┘
```

**Separation of concerns:** the server never sets up namespaces or runs untrusted code in its own process. Its one hands-on step is placing each run's helper in a cgroup from outside, before the sandbox exists. All danger is downstream in the helper's isolated children.

---

## Part 1 — The MCP server

### What MCP is (and the one rule it imposes)

MCP is JSON-RPC between an agent host and your server. For a local server the transport is **stdio**: the host launches the process and talks over stdin/stdout. Therefore **stdout is the protocol channel** — any stray `print()` corrupts the stream. All logging → stderr or a file. This is the #1 way people break an MCP server.

**Log calls never wait for stderr** (M3 step 4). A client that stops reading the server's stderr fills the pipe, and a plain log handler would then block the event loop at its next line. The server's handler (`stderr_log.py`) only puts the line in a queue, bounded at 1,000 lines of at most 16,384 characters each; one daemon thread writes the queue to its own copy of fd 2. On a full queue lines are dropped and counted, and `stderr was not being read: dropped N log line(s)` marks the gap once lines go through again. Every exit waits at most 20 ms for the queue, and the POSIX shutdown line is skipped when the queue didn't drain in that time or stderr has no room. What still writes stderr directly, all on crash paths: Python's exception hooks (an uncaught exception in a thread, an unraisable one, and on Windows one that escapes `server.run`), and the native helper when it crashes.

Flow: `initialize` handshake → host calls `tools/list` to discover tools → host calls `tools/call` to invoke one. The official `mcp` SDK handles the wire framing; you write handlers.

### Server responsibilities

1. **Protocol** — handshake, discovery, framing (SDK does the heavy lifting).
2. **Tool schemas** — the contract the model reads. This is UX design *for an LLM* (see below).
3. **Validation & policy** — reject malformed/oversized/over-limit requests before the warden sees them. First defense layer, and cheap.
4. **Invoking the warden** — call it, supervise it, enforce an *outer* deadline in case the warden itself hangs.
5. **Result shaping** — turn `{stdout, stderr, exit, usage, denied_syscalls, isolation}` into model-readable content.
6. **Audit logging** — structured record per call (stderr/file, never stdout).
7. **Config** — limits/policy from env or file so users tune without editing code.

It explicitly does **not** isolate or execute untrusted code in-process. Its only cgroup work is placing each run's helper from outside, before the sandbox starts (Part 2, stages 1–3).

### Tool schema design = UX for a model

The "user" of your schema is an LLM reading `description` fields to decide how to call the tool. Good descriptions measurably improve agent behavior.

- **Descriptions are prompts.** Tell the model the constraints ("No network access. Ephemeral filesystem except the workspace. Resource-limited.") so it doesn't waste a call trying to `pip install` and then get confused.
- **Small surface** — two tools only.
- **Constrain with enums/bounds** so invalid states are unrepresentable (`language` enum; `timeout_seconds` max advertised in schema *and* clamped server-side).
- **Make failure legible** — return "Blocked: network access is disabled in this sandbox" as content, not a bare non-zero exit. The model reads it and adapts.

### Five server design points (enforced in `CLAUDE.md`)

1. **Errors returned as content, not raised** — an unhandled throw can kill the session.
2. **Two nested timeouts** — inner = warden kills the guest (authoritative); outer = `asyncio.wait_for` protects the server from a wedged warden.
3. **Validation is a security layer, placed first** — in the clean process, before spawning anything.
4. **Truncate guest output** — bound stdout/stderr so a chatty program can't flood the model's context window (and cost).
5. **Report `isolation:` in every result** — honest transparency about what actually protected the run.

### A blocking warden, an async server

The MCP SDK is `async`, and the native warden is all blocking syscalls (`unshare`, `mount`, `fork`, `waitpid`). **None of them runs in the server's process.** Each run starts a fresh, single-threaded helper interpreter with `asyncio.create_subprocess_exec` (N1), the same call the container backend uses for `docker run` and the M0 naive backend for its subprocess. The helper makes the blocking calls; the server only reads pipes.

Forking the server instead (from an executor thread, as this document first said) was rejected: the server is multi-threaded (the event loop, the SDK's machinery), and `fork()` copies only the calling thread, so a lock another thread held stays locked forever in the child. The one blocking step left in the server, moving the helper between cgroups (the kernel takes a global lock for it), runs in a thread (`asyncio.to_thread`).

### The audit writer: one thread, one wait per call

Every `run_code` call that reaches the handler leaves records in `~/.vestibule/audit/` (`VESTIBULE_AUDIT_DIR`): a `blocked` record when it ends before a backend is chosen, otherwise a `start` and an `end` sharing one id. The event loop never touches that disk. `audit.AuditLog.submit` serializes the record and puts it on a bounded queue (1000) with an acknowledgement future, and one daemon thread per process, holding the process's one file descriptor, writes it and settles the future through `call_soon_threadsafe`. It is not `asyncio.to_thread`: stuck writes would hold workers of the default pool, which also runs native placement, `read_workspace` and the container finisher, so a hung audit disk would degrade runs. One descriptor also matters on Windows, whose C runtime emulates `O_APPEND` as a seek and a write and loses records when each write opens its own. The `start` is queued before the run and not waited for; after the run, one wait of at most 2 s covers both records. A record that doesn't land never stops the run: the result gets an `audit: not recorded (…)` or `audit: not confirmed (…)` line above `isolation:`, and a cancelled call queues its record and re-raises at once. Contract: [`M3-audit-usage.md`](plans/M3/M3-audit-usage.md) A5–A8; build: [`M3-step2-audit-writer.md`](plans/M3/M3-step2-audit-writer.md).

`vestibule-mcp audit [N]` reads those files back (`audit_cli.py`, which only that command imports, since it prints to stdout). It pairs each `start` with its `end` inside the file that holds both, and prints the last N calls merged by `ts`, one line each. A record holds code an agent sent, and a hand-edited one can hold anything, so only strings and numbers are printed, each cut to a cap, and every printed value goes through one sanitizer that escapes control, format and separator characters. Output that isn't UTF-8 is pure ASCII, because a legacy 8-bit charset puts printable characters on the bytes a terminal reads as C1 controls. Build: [`M3-step3-audit-command.md`](plans/M3/M3-step3-audit-command.md).

---

## Part 2 — The warden

The technical heart. Three processes per run, each with one job (N1, N7): the **server** places and supervises from outside; a fresh **helper** builds the sandbox and stays outside its PID namespace; **init**, PID 1 inside, seals the remaining dimensions, drops every privilege and starts the **guest**. Order matters — several operations are irreversible or must happen before a privilege boundary closes.

### Full lifecycle

```
server (async, clean)      helper (one per run)           init (PID 1)              guest
─────────────────────      ────────────────────           ────────────              ─────
1. spawn the helper,
   paused                  PDEATHSIG; waits
2. systemd scope for it;
   guest limits, read back
3. release ─────────────▶  4. compile the filter
                           5. unshare(USER|PID);
                              identity maps
                           6. fork ─────────────────────▶ 7. wait for "go"
                              init → guest/sandbox; go
                                                          8. setsid; unshare(NS|
                                                             NET|UTS|IPC|CGROUP)
                                                          9. jail, lo up,
                                                             pivot_root
                                                          10. drop caps; not
                                                              dumpable; NNP
                                                          11. load the filter;
                                                              listener → helper
                                                          12. self-checks → ready
                           13. relay status; answer       14. fork ────────────▶    15. execve
                               notifications (EPERM)          reap; report exit
16. collect output (capped);
    timeout: SIGTERM ────▶ 17. SIGKILL init via
                               its pidfd: the PID ns dies
                           18. after init: OOM counts,
                               usage, denied list; exit
19. verdict from the report;
    systemd drops the scope
```

A failure in steps 4–11, or in one of init's own checks in step 12, means the guest never runs: init reports the stage that failed, and the result says `isolation: none … nothing was executed`. The checks the server makes on the `ready` report are judged after the run; one that fails makes the result `isolation: none` (stage 12–19). A failure in step 2 only loses the limits: the run goes ahead as `native-degraded`, naming what is missing and why (N5).

### Stages 1–3 — Limits first, placed from outside (cgroups v2 through systemd)

cgroup v2 lets a process be moved only by someone who can write the common ancestor's `cgroup.procs`, and an unprivileged user owns nothing under `/sys/fs/cgroup` except what systemd delegates. A sandbox also can't join a cgroup itself after `pivot_root`: the cgroup filesystem isn't in its view. So the server does it, from outside, while the helper waits paused on a pipe (N3):

- It asks the user's systemd manager (`busctl --user call … StartTransientUnit`) for a transient scope, `vestibule-<run>.scope`, holding only the helper, with `Delegate=yes`. The scope's own properties are the helper's *envelope*, the guest's limits plus a margin (+25 % of a CPU, +64 MiB, +16 tasks), and an outside deadline, `RuntimeMaxSec` with `SIGKILL`, that no guest can stop.
- Under the scope, `supervisor/` takes the helper (a cgroup that hands controllers to its children can't hold processes itself), and `guest/sandbox/` will take init. The guest's limits go on `guest/`. cgroups v2 is just filesystem writes, no ctypes:

```python
# native_cgroup.py (abridged): M1's values, written once the subtree is delegated
wanted = {
    "cpu": {"cpu.max": f"{cpu_pct * 1000} 100000"},   # what Docker writes for --cpus
    "memory": {"memory.max": str(mem_mb << 20), "memory.swap.max": "0"},
    "pids": {"pids.max": str(pids_max)},              # fork-bomb containment
}
for name, files in wanted.items():
    _write(f"{scope}/cgroup.subtree_control", f"+{name}")
    for file, value in files.items():
        _write(f"{scope}/guest/{file}", value)
    # counted as applied only if every file reads back what was written
```

- The release message names the cgroup for init. Any failure here (no user bus, a controller not delegated, a refused property, more than 5 s) only degrades the run, and the result's detail says which limits are missing and why. Limits are the soft tier; the namespaces, the jail and seccomp are the hard tier (N5).
- Sandboxes therefore live in the user manager's `app.slice`, outside the agent client's own cgroup. When the helper exits, the scope is empty and systemd removes it. Nothing is left to tear down, with one exception. A placement cut short (a cancel) or one that found the helper gone can leave a scope that systemd made for a helper that had already exited. Such a scope is born empty and never empties, so the finisher stops it by name once the helper is gone.

### Stages 4–6 — The helper: a private user namespace, then init

Released, the helper compiles the seccomp filter (stage 11) while the standard library and ctypes are still reachable. After `pivot_root` nothing new can be imported. Then it unshares a user and a PID namespace and writes the maps:

```python
# native_helper.py (abridged)
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID  = 0x20000000

uid, gid = os.getuid(), os.getgid()
_check(libc.unshare(CLONE_NEWUSER | CLONE_NEWPID), "unshare")

# ORDER IS MANDATORY when unprivileged: setgroups first, or the gid_map write fails (EPERM)
_write("/proc/self/setgroups", "deny")
_write("/proc/self/uid_map", f"{uid} {uid} 1")   # identity: the guest is you, not root
_write("/proc/self/gid_map", f"{gid} {gid} 1")
pid = os.fork()                                  # the child is PID 1 of the new PID namespace
```

- **An identity map, not `0 <uid> 1`.** Mapping your uid to 0 would make the guest root inside its namespace. With the identity map the helper and init still hold a full capability set *inside* the new user namespace, which is what lets init mount and `pivot_root`, and none outside it. The guest is the server's own uid (you, never root: selection refuses a server started as root, for both backends, since the container's `--user` copies the same uid), its capabilities are dropped before `execve`, and files it writes to the workspace are owned by you (N6). Your supplementary groups still count, since no unprivileged process can drop them, but nothing they unlock is visible in the jail.
- **Why the helper unshares only user and PID.** `unshare(CLONE_NEWPID)` doesn't move the caller: the *next* `fork()` becomes PID 1 of the new namespace. So the helper stays outside the PID namespace it supervises, where the guest can neither see nor signal it, and init does the other namespaces itself.
- The helper moves init into `guest/sandbox/` before sending it "go", so init is limited before it does anything. `PR_SET_PDEATHSIG(SIGKILL)` ties the helper to the server and init to the helper, and the kernel kills every process in a PID namespace when its PID 1 dies.

### Stages 7–9 — init: the other namespaces and the filesystem jail

init waits for "go", starts its own session (no controlling terminal, so no way to the server's `/dev/tty`), then unshares the rest: `CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWCGROUP`. The cgroup namespace makes the guest's `/proc/self/cgroup` read `/` instead of the host's path.

**The jail** is built under the rootfs, then entered with `pivot_root` (the stronger form of chroot: the old root is detached after, so no path leads back to it):
- **The rootfs** is an unpacked copy of the same digest-pinned images the container backend runs (`python:3.12-slim`, `node:22-slim`). `vestibule-mcp setup` fetches it once (N2), and every run bind-mounts it read-only. No host binaries, no download per run, no temporary rootfs.
- **A fresh `/proc`** reflecting the sandboxed process tree, with Docker's masked and read-only paths. A minimal `/dev` (`null`, `zero`, `full`, `random`, `urandom`, `tty`, a new `devpts`, a 64 MiB `/dev/shm`), and no `/sys`.
- **`/tmp`**, a size-capped tmpfs; **`/workspace`**, the one bind-mounted host folder and the only file channel in and out; **`/sandbox`**, a per-run tmpfs that init writes the script into, then remounts read-only.
- `~/.ssh`, `~/.aws` and `.env` are simply *not present* in the guest's filesystem. Exfiltration is structurally prevented, not policy-blocked.

**The network.** A new network namespace with no veth pair has only a loopback interface. init brings `lo` up, as Docker's `--network none` does; nothing is ever bridged to the host, and that is the "no network egress" guarantee. It is not the whole story: a network namespace doesn't isolate `AF_VSOCK` sockets (on WSL a sandbox's vsock `connect` reaches the Hyper-V transport to Windows), so the seccomp filter refuses that socket family (stage 11).

### Stage 10 — Drop every privilege (PID 1 hardened against its own guest)

init and the guest share a uid, so without hardening the guest could open `/proc/1/*`, read init's environment, or forge its status report (N7). In order:
- **fds:** 0 is `/dev/null`, 1 and 2 are the guest's pipes, 3 is the status socket, and everything else is closed (`close_range`).
- **Capabilities:** the bounding set dropped, the ambient set cleared, then `capset` to nothing.
- **`PR_SET_DUMPABLE 0`,** which makes `/proc/1/*` unreadable to the guest.
- **`PR_SET_NO_NEW_PRIVS 1`,** the prerequisite for an unprivileged seccomp filter.
- **Without a pids limit,** on Linux 5.14 and later, an `RLIMIT_NPROC` backstop against fork bombs. (Before 5.14 the kernel counts that limit across all of the user's processes, so it can't serve as a per-sandbox cap.)

### Stage 11 — Lock the syscall surface (seccomp)

**Do not hand-assemble BPF.** Vestibule has its own small ctypes binding over the system's `libseccomp.so.2`, and libseccomp compiles the filter (N4). The allowlist is derived from Docker's default profile, vendored in the repo at a pinned commit. It is **required**: without libseccomp, native isn't used.
- **Default action `EPERM`,** as in Docker's profile.
- **A named set of dangerous calls** (`mount`, `unshare`, `setns`, `bpf`, `keyctl`, `reboot`, the module and kexec calls, `socket(AF_VSOCK)`, and a few more) goes to a *user notification* instead. The helper holds the listener, answers `EPERM` and records the name, so the result can say `blocked syscalls: mount`. After 256 notifications it closes the listener; the rest then fail with `ENOSYS`, and the result says reporting stopped.
- **`socket` and `socketpair`** only for `AF_UNIX`, `AF_INET`, `AF_INET6` and `AF_NETLINK`.
- **`ptrace` stays allowed,** as in Docker (S5-D1), so debuggers work inside the sandbox.
- **A syscall from a foreign architecture kills the process** (the filter's bad-arch action).

Ordering: the filter is compiled in the helper before the fork, so every ctypes type exists before `pivot_root`, and init loads it **last**, after `no_new_privs`, so its own setup isn't caught. init is filtered too, and the guest inherits the filter.

### Stages 12–19 — Check, run, supervise, stop, report

- **Checks before the guest exists.** init verifies what it reads from the kernel: the mount flags (`ro,nosuid,nodev` on the rootfs and `/sandbox`), every capability set 0, `NoNewPrivs: 1`, not dumpable, only `lo`, no controlling terminal. Any of these failing aborts before `execve`. Then it reports `ready` with those results, its `Seccomp:` mode, namespace inodes, identity maps, mount table and cgroup, and forks the guest. The server judges the rest when the run is over: the seccomp mode must be 2 (filtering), and the namespaces, maps and mount table must match its own values. If any of those fails, the result says `isolation: none`, never `native`. `isolation: native` means every check held, for this run.
- **The guest** is forked by init: fds above 2 closed, default signals, its own `PATH` search, then `execve` of the interpreter. init stays as a minimal reaper, like M1's `--init`, and reports the guest's exit.
- **Output** flows over pipes to the server, capped as in M1; a run printing more than twice the cap is stopped.
- **The stop** (timeout or output overflow): the server sends `SIGTERM` to the helper, which `SIGKILL`s init through a pidfd, so a reaped PID can't be reused under it. When PID 1 dies, the kernel kills everything in its PID namespace, stopped processes included, so no guest can block the stop. The helper exits only after init has, so its exit means the sandbox is empty; `SIGKILL` to the helper 5 s later is the fallback. Two deadlines outside the server back this up: the helper's own self-deadline and systemd's `RuntimeMaxSec` on the scope.
- **The report.** Before it exits, the helper sends the guest cgroup's OOM counters (so a memory bomb's result says the kernel killed a process at its limit) and the denied syscalls' names. It also sends the usage counters (M3): `cpu.stat`, `memory.peak`, `pids.peak` and `pids.events`, read from the guest cgroup after init has exited. So they are final, and they come from outside the sandbox, where the guest can't fake them. A timed-out run waits up to 1 s more for them. They count the whole sandbox, init's own setup and the page cache included, so the same code can read differently twice (`print(1)` in python: 14 MiB after the page cache is cleared, 6 MiB right after another run). A result without them names what it didn't measure, and why. Once the helper has exited, the scope is empty and systemd removes it; a scope that a placement cut short may have left is stopped by name instead (stages 1–3).

---

## Honest scope (put this in SECURITY.md)

Vestibule gives strong isolation comparable in *mechanism* to rootless containers. It is **not** a hardened VM boundary — it shares the host kernel, so a kernel privilege-escalation exploit could in principle escape (VMs like gVisor/Firecracker defend that layer). State this plainly. The goal: make the *common, realistic* agent risks — prompt-injected exfiltration, destructive commands, resource exhaustion — structurally impossible. Stating the limits honestly is a trust-builder and exactly the threat-model maturity that impresses in interviews. Never claim "unescapable."
