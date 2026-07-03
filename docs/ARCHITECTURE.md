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
                                                          │ in-process call → executor
                                                          ▼
                                          ┌───────────────────────────────┐
                                          │      Warden (Python + ctypes)  │
                                          │  isolation + execution core     │
                                          │                                 │
                                          │  unshare(NEWUSER|NEWNS|NEWPID|  │
                                          │          NEWNET|NEWUTS|NEWIPC)  │
                                          │  uid/gid map → fork() (PID 1)   │
                                          │  mount /proc → pivot_root       │
                                          │  cgroup v2: mem/cpu/pids caps   │
                                          │  no_new_privs → seccomp → exec  │
                                          └───────────────┬───────────────┘
                                                          ▼
                                                 ┌─────────────────┐
                                                 │  Guest process  │
                                                 │ (agent's code)  │
                                                 └─────────────────┘
```

**Separation of concerns:** the server never sets up namespaces, touches cgroups, or runs untrusted code in its own process. All danger is downstream in the warden's isolated child.

---

## Part 1 — The MCP server

### What MCP is (and the one rule it imposes)

MCP is JSON-RPC between an agent host and your server. For a local server the transport is **stdio**: the host launches the process and talks over stdin/stdout. Therefore **stdout is the protocol channel** — any stray `print()` corrupts the stream. All logging → stderr or a file. This is the #1 way people break an MCP server.

Flow: `initialize` handshake → host calls `tools/list` to discover tools → host calls `tools/call` to invoke one. The official `mcp` SDK handles the wire framing; you write handlers.

### Server responsibilities

1. **Protocol** — handshake, discovery, framing (SDK does the heavy lifting).
2. **Tool schemas** — the contract the model reads. This is UX design *for an LLM* (see below).
3. **Validation & policy** — reject malformed/oversized/over-limit requests before the warden sees them. First defense layer, and cheap.
4. **Invoking the warden** — call it, supervise it, enforce an *outer* deadline in case the warden itself hangs.
5. **Result shaping** — turn `{stdout, stderr, exit, usage, denied_syscalls, isolation}` into model-readable content.
6. **Audit logging** — structured record per call (stderr/file, never stdout).
7. **Config** — limits/policy from env or file so users tune without editing code.

It explicitly does **not** isolate, touch cgroups, or execute untrusted code in-process.

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

### Sync warden vs. async server

The MCP SDK is `async`; the native warden does blocking syscalls (`fork`, `waitpid`, file writes). **Never run blocking fork/exec directly in the event loop** — it stalls the server. Native warden → run in an executor (`loop.run_in_executor(...)`). Container backend → use `asyncio.create_subprocess_exec` (already async). The M0 naive backend uses `create_subprocess_exec` and needs no executor.

---

## Part 2 — The warden

The technical heart. Sequence: *become root in a private world → seal every dimension → drop in → run the guest.* Order matters — several operations are irreversible or must happen before a privilege boundary closes.

### Full lifecycle

```
Parent (warden)                          Child (becomes the guest)
───────────────                          ─────────────────────────
1. create cgroup, set limits
2. unshare(NEWUSER|NEWNS|NEWPID
        |NEWNET|NEWUTS|NEWIPC)
3. write uid_map / gid_map
   (now "root" inside the userns)
4. fork()  ─────────────────────────────▶ 5. child is PID 1 in new PID ns
                                           6. mount fresh /proc
                                           7. pivot_root into minimal rootfs
                                           8. join cgroup (write cgroup.procs)
                                           9. prctl(PR_SET_NO_NEW_PRIVS, 1)
                                          10. install seccomp-bpf allowlist
                                          11. drop remaining capabilities
                                          12. execve(interpreter, [code])
   13. wait4(); enforce wall-clock timeout
   14. read cgroup peak usage, denied syscalls
   15. tear down cgroup + temp rootfs
   16. return {stdout, stderr, exit, usage} ◀── (streamed via pipe)
```

### Stage 1–3 — Become root in a private world (user namespace)

The key trick for unprivileged operation: `unshare(CLONE_NEWUSER)` creates a namespace where the unprivileged real UID maps to UID 0 *inside*. Full capabilities inside, zero outside. (Same mechanism as rootless Podman / bubblewrap.)

**Footgun:** when unprivileged, write `deny` to `/proc/self/setgroups` **before** writing `/proc/self/gid_map`, or the gid_map write fails with `EPERM`.

```python
import os, ctypes
libc = ctypes.CDLL("libc.so.6", use_errno=True)

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS   = 0x00020000
CLONE_NEWPID  = 0x20000000
CLONE_NEWNET  = 0x40000000
CLONE_NEWUTS  = 0x04000000
CLONE_NEWIPC  = 0x08000000

def _unshare(flags: int) -> None:
    if libc.unshare(flags) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), "unshare")

real_uid, real_gid = os.getuid(), os.getgid()
_unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID |
         CLONE_NEWNET | CLONE_NEWUTS | CLONE_NEWIPC)

# ORDER IS MANDATORY when unprivileged:
with open("/proc/self/setgroups", "w") as f:
    f.write("deny")
with open("/proc/self/uid_map", "w") as f:
    f.write(f"0 {real_uid} 1")     # inside-uid 0 -> outside real_uid
with open("/proc/self/gid_map", "w") as f:
    f.write(f"0 {real_gid} 1")
```

`CLONE_NEWNET` with no veth pair created = the guest gets a network namespace with only a down loopback interface. **That is the "no network egress" guarantee, and it's free** — you simply never bridge it to the host. Stronger and simpler than firewall rules.

### Stage 4–5 — fork() and the PID namespace

Subtlety: `unshare(CLONE_NEWPID)` does **not** move the caller into the new PID namespace — it arranges for the *next* `fork()`'d child to be **PID 1** there. So: unshare, then fork; the child wakes as PID 1 in a world where it can't see or signal host processes. This is exactly the fork/exec pattern from mysh, with namespace flags flipped on first. (Being PID 1 means reaping responsibilities/different signal semantics — fine for a short-lived single command; just don't leave the child hanging.)

### Stage 6–7 — Filesystem jail (mount ns + pivot_root)

The mount namespace gives the child a private mount view. Two operations:

- **Mount fresh `/proc`** — the guest needs a `/proc` reflecting the *sandboxed* process tree, not the host's.
- **`pivot_root`** into a minimal rootfs (the strong, non-escapable version of chroot; unmount the old root after). Guest sees only: read-only minimal rootfs (busybox + interpreter), writable `/tmp` (size-capped tmpfs), and the single bind-mounted **workspace** dir (the only file channel in/out). `~/.ssh`, `~/.aws`, `.env` are simply *not present* in the guest's filesystem — exfiltration is structurally prevented, not policy-blocked.

**Rootfs options** (easiest first): (a) bind-mount host `/usr`, `/lib` read-only + tmpfs overlay — fast, no download, but exposes host binaries; (b) ship a tiny busybox + static-Python rootfs tarball per run — cleaner isolation; (c) reuse an OCI image layer. Start with (a) read-only for MVP; move to (b) for the security-story version.

### Stage 8 — cgroups v2 (resource cage)

Created by the parent in stage 1 (while it has permission); the child joins by writing its PID to `cgroup.procs`. cgroups v2 is just filesystem writes — no ctypes:

```python
import os
CG_ROOT = "/sys/fs/cgroup"

def make_cgroup(name: str, mem_mb: int, cpu_pct: int, pids_max: int) -> str:
    cg = f"{CG_ROOT}/vestibule/{name}"
    os.makedirs(cg, exist_ok=True)
    _write(f"{cg}/memory.max", str(mem_mb * 1024 * 1024))  # mem bomb -> OOM guest, not host
    _write(f"{cg}/pids.max",  str(pids_max))               # fork-bomb containment
    _write(f"{cg}/cpu.max",   f"{cpu_pct * 1000} 100000")  # quota period, e.g. 75% of a core
    return cg

def join_cgroup(cg: str) -> None:
    _write(f"{cg}/cgroup.procs", str(os.getpid()))
```

After the run, `memory.peak` and `cpu.stat` give the high-water marks reported in the result (the observability angle). **Gotcha:** unprivileged cgroup control needs cgroup v2 with delegation — common but not universal. Degrade gracefully ("limits unavailable, reduced guarantees") rather than hard-fail, and report it in `isolation:`.

### Stage 9–11 — Lock the syscall surface

- **`no_new_privs`** — `prctl(PR_SET_NO_NEW_PRIVS, 1)`. Prerequisite for unprivileged seccomp; one line.
- **seccomp-bpf** — the syscall allowlist. **Do not hand-assemble BPF.** Use optional `pyseccomp`: allow `read/write/mmap/brk/exit/exit_group/…`; deny (`EPERM` or kill) `socket`, `ptrace`, `mount`, `keyctl`, `reboot`, etc. Keep degradable — namespaces + cgroups already give strong isolation; seccomp is defense-in-depth on top.
- **Drop capabilities** — even as userns-root, drop the ambient/bounding set.

Ordering: `no_new_privs` **before** seccomp; seccomp **last, right before `execve`**, so your own setup code isn't caught by the filter.

### Stage 12–16 — Run, supervise, report, tear down

Child `execve`s the interpreter with the code. Parent `wait4`s while enforcing a **wall-clock timeout** (a separate alarm/timer — CPU limits alone don't catch `sleep(99999)`). stdout/stderr flow back over pipes (same plumbing as mysh). On completion/timeout: kill the process group, read peak usage from cgroup files, record denied syscalls, tear down cgroup + temp rootfs, return the structured result to the server.

---

## Honest scope (put this in SECURITY.md)

Vestibule gives strong isolation comparable in *mechanism* to rootless containers. It is **not** a hardened VM boundary — it shares the host kernel, so a kernel privilege-escalation exploit could in principle escape (VMs like gVisor/Firecracker defend that layer). State this plainly. The goal: make the *common, realistic* agent risks — prompt-injected exfiltration, destructive commands, resource exhaustion — structurally impossible. Stating the limits honestly is a trust-builder and exactly the threat-model maturity that impresses in interviews. Never claim "unescapable."
