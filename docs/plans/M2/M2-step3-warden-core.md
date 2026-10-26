# M2 step 3 — Warden core: the helper, the jail, PID-1 hardening, the kill path

Status: **signed off by Victor 2026-10-25** (v3, revised after two `skeptic` reviews, §7).
- **Victor's decision:** S3-D1, the script in a per-run tmpfs, approved as recommended.
- **Contract text this plan amends, under the same sign-off:**
  - N1: the helper's launch flags and environment (S3-D4);
  - N5's open label for a run with no status (S3-D8);
  - N8's kill order without cgroups (S3-D7);
  - N9: `/proc` masks, loopback up, no controlling terminal (S3-D2, S3-D3, S3-D7);
  - §4.2: the script (S3-D1);
  - spike A4 and A5 (S3-D4, S3-D5).
- The other decisions are marked *fixed*: one defensible answer each, most of them forced by
  a measurement in §1.

Contract: [`M2-native-warden.md`](M2-native-warden.md) N1, N5–N9, N11, §3, §4 and §10 step 3.
One commit.

**What this step delivers:** `NativeBackend`, which runs one `run_code` call in a fresh
sandbox built from the step-2 rootfs:
- user, PID, mount, network, UTS, IPC and cgroup namespaces;
- the `pivot_root` jail, with `/workspace`, `/sandbox`, `/tmp`, `/dev` and `/proc`;
- PID 1 hardened against its own guest;
- a kill path that a stopped or hostile guest can't block.

**Not reachable from the server yet:** only tests build the backend (contract §10 step 3).

**Not in this step** (Karpathy "simplicity first"; each is a later step's job):
- **Step 4:** cgroups, limits, `cgroup.kill` and `RLIMIT_NPROC`.
- **Step 5:** seccomp and denied-syscall reporting, which is also what closes `AF_VSOCK`. In this
  step a sandbox's vsock still reaches the Windows host; native stays unreachable until step 6,
  after step 5 has closed it.
- **Step 6:**
  - selection, the probe and the actionable messages;
  - the per-run overlap check (S2-D9);
  - the `native` and `container` backend values;
  - the selector's shared semaphore and shutdown tracking.
- **Step 7:** the README, `ARCHITECTURE.md`, the `CLAUDE.md` wording and the benchmark.
- **Left out entirely:**
  - a fresh kernel session keyring per run: `/proc/keys` is masked here (S3-D2), and step 5's
    filter denies the keyring syscalls (N4);
  - `/sys` and `/dev/mqueue` (N9 has no `/sys`; POSIX message queues work without the mount);
  - renaming init's `comm`: it shows `python3.14`, with no path.

## 1. What the investigation found (2026-10-25, before any code)

A throwaway prototype of this step's helper and server side ran against the real step-2 trees
in `~/.vestibule/rootfs`, on the gate's interpreters: the system 3.14, the 3.14 venv and uv's
3.11. The scripts are kept in WSL at `~/m2spike/step3/`, next to step 0's:
- `proto_warden.py` and `proto_server.py`: the first prototype;
- `*_norec.py`: the non-recursive binds;
- `proto2_*.py`: the v3 design;
- the `probe_*.sh` scripts.

| Question | Result |
|---|---|
| Spike A4's launch (`-I`, neutral `argv[0]`), on all three interpreters | Starts, but none of them finds its real prefix. Both 3.14s fall back to `/` and find the stdlib only through Ubuntu's `/lib → usr/lib` link. uv's 3.11 can't find `lib-dynload`, and prints `Could not find platform dependent libraries <exec_prefix>` on stderr. The spike missed it because its check printed stderr only when stdout was empty. |
| The same with `PYTHONHOME=<base prefix>:<base exec prefix>` and `-S -P` | Clean on all three: the right prefix, nothing on stderr, and no site-packages directory on `sys.path`. `/proc/<pid>/cmdline` is `vestibule-warden -S -P -c exec(open(N,'rb').read())`. |
| Imports after `pivot_root` | On 3.14, the first `ctypes.Structure` subclass imports `ctypes._layout`, so init failed at its "caps" stage (spike A5's accident, again). On both versions, `os.execvpe` imports `warnings`; 3.14 survived only because another module had imported it already. |
| `os.closerange(3, 2**20)` | 3.14: 0.0 ms (it uses `close_range`). uv's 3.11: **246 ms**; it loops, and two calls made every 3.11 run take ~450 ms. The `close_range` syscall itself: 0.05 ms. |
| Docker `--network none`, loopback | `lo` is UP (flags `0x49`); TCP to `127.0.0.1` works. The spike's sandbox left `lo` down. |
| Docker's `/proc` masks (28.5.1, this host) | Masked: `acpi`, `interrupts`, `kcore`, `keys`, `latency_stats`, `timer_list`, `scsi` (and `/sys/firmware`). Read-only: `bus`, `fs`, `irq`, `sys`, `sysrq-trigger`. |
| Native `/proc` without masks | `/proc/keys` lists the user's kernel keyrings, including the session keyring the sandbox inherited. `/proc/interrupts` and `/proc/latency_stats` are readable; `timer_list` and `kcore` are already `0400`. Under `/proc/sys`, only `ns_last_pid` is writable. With Docker's list applied, all are empty and nothing is writable. |
| Locked mount flags | A workspace on a `noexec` or read-only mount, made in an outer user namespace as a host's own would be: the `nosuid,nodev` remount fails with `EPERM` (setup stops; the guest never runs) unless it carries `noexec`/`ro` over. A `noatime` mount (`/mnt/c`) needs nothing, because the kernel keeps atime flags on a remount. |
| A mount inside the workspace | With the spike's recursive bind, a tmpfs mounted inside the workspace appeared in the sandbox at `/workspace/sub` as `rw,relatime`, **without** `nosuid,nodev`: a remount changes only the top mount. A non-recursive bind refuses it (`EINVAL`), so setup stops. Without a mount inside, non-recursive binds of the tree and the workspace work exactly like recursive ones. |
| The controlling terminal (second `skeptic` review) | When the server has a terminal (started from a shell), the guest inherited it as its controlling terminal and could open the bind-mounted `/dev/tty` read-write. Docker, from the same pty, gives `ENXIO`. With `setsid()` in init, the guest has `tty_nr 0` and gets `ENXIO`, under a pty too (both interpreters). |
| Signals | The guest inherited SIGPIPE and SIGXFSZ as *ignored*: Python sets that at start-up, and `execve` keeps it. `yes \| head -1` printed `Broken pipe`. A guest's `kill -INT 1` interrupted init through Python's SIGINT handler and ended the run. |
| Stopping without cgroups: SIGTERM to the helper, which SIGKILLs init | The helper was gone 3.3–5 ms after the signal, with nothing left: a busy loop, a self-`SIGSTOP`ped guest, a background child, and a guest that closed its stdout/stderr and kept running. |
| The script in a per-run tmpfs at `/sandbox` | Works. Init writes it and remounts the tmpfs `ro,nosuid,nodev`; the guest sees `/sandbox/main.py`, and its writes get `EROFS`. |
| The v3 launch: config on a memfd as stdin, the helper's source by fd, the guest's pipes as extra fds, the helper's stdout `/dev/null` and its stderr the server's | Works with `asyncio.create_subprocess_exec(..., executable=, stdin=<fd>, pass_fds=)` on 3.11 and 3.14. The tree can be bound read-only from the start, and every later mount goes on top of it. `/proc/self/mountinfo` shows each tmpfs's `size=` in its super options. |
| Cold start (python `print(1)`, no cgroups, 20 runs) | Median 36–50 ms on 3.14 and 40 ms on 3.11; node 50–240 ms. |
| Cold page cache (the tree and the host's stdlib evicted with `posix_fadvise`) | node `console.log` 182–191 ms (warm: 56–61 ms); python 83 ms. Windows may still cache the WSL disk, so a truly cold disk can be slower. |
| What the guest sees | uid/gid 1000, `groups=1000,65534(nogroup)` (the host's supplementary groups show as the overflow gid, N6's documented gap). `/proc/1/{environ,cwd,root,fd,mem,exe,maps}` are `EACCES`; fds `{0,1,2}`; stdin `/dev/null`; `localhost` resolves; TCP to `1.1.1.1` gives `ENETUNREACH`. |

**Assumptions** (Karpathy "think before coding"; each is checked or made loud):
- **Kernel ≥ 5.14.** `cgroup.kill` (step 4) needs it anyway; this step needs 5.9 for
  `close_range` and 5.3 for `pidfd_open`. An older kernel fails setup with the stage named.
- **The server's own interpreter runs the helper,** so it is ≥ 3.11 and has a stdlib at
  `sys.base_prefix`.
- **The rootfs trees are step 2's,** found with `rootfs.find()`, and read-only to the guest.
- **The host's `/proc` is fully visible.** A fresh procfs in a user namespace needs that, so
  inside a container or under systemd's `ProtectProc=`, setup fails at the mounts stage (§5).
- **amd64 is tested for real.** arm64 has its `pivot_root` syscall number (41) but is
  unverified, as in step 2.

## 2. Decisions

### S3-D1 (Victor; amends contract §4.2) — The script goes into a per-run tmpfs at `/sandbox`, not a host temp dir

The contract says each run gets "a host temp dir for the script (as D9)", bind-mounted
read-only at `/sandbox`. Instead:
- The server passes the code in the run's config, an anonymous memfd on the helper's stdin.
- Init mounts a fresh tmpfs on the tree's `sandbox/`, writes `main.<ext>` into it, and
  remounts it `ro,nosuid,nodev` before `pivot_root`.
- The guest sees the same thing: `/sandbox/main.<ext>`, read-only.

**Why:**
- **Nothing on the host backs `/sandbox`,** so a run creates no host files at all. There's no
  detached `rmtree`, and nothing is left behind when the server is hard-killed. BUILD_LOG's
  S7-D1 issue (leftover `vestibule-run-*` dirs) doesn't exist for native.
- **Step 6 has less to do:** a native backend's `cleanup_snapshot()` has nothing to hand to the
  cleaner (contract §4's 1b amendment expected "temp dirs only").
- **D9's three reasons still hold:** the script is not in argv, not on the guest's stdin, and
  not in the workspace.

**Rejected:** the host temp dir, as the contract has it. It costs a cleanup task, a leak on hard
kill, and one more host path in the guest's `/proc/self/mountinfo`.

### S3-D2 (fixed; amends N9) — `/proc` gets Docker's masks

N9 aims for parity with the container profile. Docker hides parts of `/proc`, and §1 shows
why it matters here: `/proc/keys` lists the user's kernel keyrings.
- **Masked**, if present:
  - files get a bind mount of `/dev/null`: `interrupts`, `kcore`, `keys`, `latency_stats`,
    `timer_list`, `timer_stats`, `sched_debug`;
  - directories get an empty read-only tmpfs: `acpi`, `asound`, `scsi`.
- **Read-only:** `bus`, `fs`, `irq`, `sys`, `sysrq-trigger`.
- **The list is what Docker 28.5.1 applied on this host (§1),** minus `/sys` (N9 has none).
  Paths this kernel lacks are skipped, as Docker skips them.

### S3-D3 (fixed; amends N9) — `lo` is brought up

Docker's `--network none` has `lo` up (§1), so a guest can test a server on `127.0.0.1`.
S2-D10's `/etc/hosts` assumes it too. Init sets `IFF_UP` on `lo` (one `ioctl`) before it drops
its capabilities. The hard check "only `lo` exists" stays.

### S3-D4 (fixed; amends N1 and spike A4) — How the helper is started

```
argv:        vestibule-warden -S -P -c "exec(open(<src fd>,'rb').read())"
executable:  realpath(sys.executable)
env:         {"PYTHONHOME": "<sys.base_prefix>:<sys.base_exec_prefix>"}
cwd:         /
stdin:       the run config: a memfd holding JSON (paths, env, argv, the code, fd numbers)
stdout:      /dev/null
stderr:      the server's own stderr (its log)
pass_fds:    <src fd> (the package's native_helper.py, opened read-only); the status pipe;
             the write ends of the guest's stdout and stderr pipes
```

- **`PYTHONHOME`** gives the interpreter its prefix directly. A4's `-I` with a neutral
  `argv[0]` left it guessing (§1).
- **`-S`** keeps every site-packages directory off `sys.path`, so A4's "stdlib only" rule is
  enforced, not just followed.
- **`-P`** keeps the working directory off it.
- **`-I` is dropped** because it ignores `PYTHONHOME`. N1's "minimal environment" now holds
  exactly one variable, `PYTHONHOME`, and the environment is otherwise ours entirely, so the
  `-E` that `-I` implies adds nothing.
- **The guest's output pipes are not the helper's stdout and stderr.** Init puts them on fds 1
  and 2 just before the guest forks.
  - Anything the helper interpreter itself prints goes to the server's log, never to the
    model. That covers start-up warnings from a Python build nobody tested, which is §1's
    uv-3.11 line (second `skeptic` review, finding 2).
  - stdout stays `/dev/null`, per golden rule 1.
- **The helper reads its config to EOF,** so the only bound on it is the server's
  `max_code_bytes` check (first review, finding 4).
- **`/proc/1/cmdline` shows no host path** (§1, criterion 10). It is one channel only: the
  guest still learns the username from `/proc/self/mountinfo` (§5).
- **Rejected:**
  - the real interpreter path as `argv[0]`: it fails criterion 10;
  - re-executing a reaper from the rootfs: the node image has no Python.

### S3-D5 (fixed; generalises spike A5) — After `pivot_root`, nothing that can import

The host's stdlib is out of reach after `pivot_root`, and §1 found three stdlib calls that
import lazily or misbehave. The rules:
- **Every `ctypes` type is defined at module level,** so it exists before `pivot_root`.
- **The guest's PATH search is our own loop over `os.execve`,** not `os.execvpe`.
- **fds are closed with the `close_range` syscall,** not `os.closerange`.
- **Only `os`/`posix` calls, calls on predefined `ctypes` types, `socket.sethostname`,
  `fcntl.ioctl` and `json.dumps`** run after `pivot_root`.

What catches a new lazy import: the native tests run on both 3.14 and 3.11 in the Linux gate
(the full run and the `not docker` run).

### S3-D6 (fixed) — Binds are non-recursive, read-only at once where they can be, and carry locked flags

**Non-recursive** (`MS_BIND` without `MS_REC`), for the tree and the workspace. A recursive
bind (the spike's) brings any mount inside the source along, and the `nosuid,nodev` remount
reaches only the top one (§1: a tmpfs inside the workspace showed up without either).
- A non-recursive bind of a directory with a host mount inside it is refused by the kernel
  (`EINVAL`), so setup stops and nothing runs.
- For the workspace, the error says so: "the workspace has another filesystem mounted inside
  it, which a native sandbox can't use". A bare `EINVAL` would explain nothing.
- **This differs from Docker,** whose `-v` binds recursively and shows such mounts to the
  container. Refusing is the simpler rule, and it fails closed.

**The tree is read-only from its first remount** (second review, finding 10). Every write init
makes goes to a tmpfs it mounted on top (§1), so no bug in init can write into the shared
image that every run uses.

**Locked flags.** In a user namespace, a mount that came from the host is *locked*: a remount
can't drop its `ro` or `noexec` (§1: `EPERM`, and setup stops).
- **Each bind is remounted with `ST_RDONLY` and `ST_NOEXEC` from `statvfs()` of its source
  added:** the tree, the workspace, and the read-only `/proc` paths.
- **Atime needs nothing:** the kernel keeps it on a remount.
- **Consequences, both matching the container backend:**
  - a workspace on a read-only filesystem is read-only in the sandbox, even with
    `VESTIBULE_WORKSPACE_RO` off;
  - a workspace on a `noexec` mount is `noexec` in the sandbox. Scripts still run, because
    the interpreter is exec'd from the tree; `./script.sh` from the workspace fails, as on the
    host.

### S3-D7 (fixed; implements N7, amends N8's order without cgroups) — Process shape and the kill path

**The helper** (the supervisor; it never `execve`s):
1. Sets `PR_SET_PDEATHSIG(SIGKILL)`, then exits if `getppid()` is no longer the server.
2. Unshares `NEWUSER|NEWPID` (spike A1).
3. Writes `setgroups deny` and the identity maps (N6).
4. Forks init over a socketpair, then sends it "go".
5. Closes its copies of the guest's pipes, so they reach EOF only when the sandbox is gone.
6. Opens init's pidfd, and installs a SIGTERM handler that SIGKILLs init through it
   (`signal.pidfd_send_signal`). Through a pidfd, a SIGTERM that arrives after init has been
   reaped can't hit a reused PID (second review, finding 9).
7. Polls the pidfd and the socketpair, relaying init's status lines. A self-deadline of
   `timeout_s + 30` SIGKILLs init if the server never stops the run (N8's last backstop).
   *(Build: it writes nothing to stderr when it fires. The server's stderr may be the stalled
   pipe that stopped the server, and a write there would block before the stop; second
   `critic` round.)*
8. Writes its last status line and exits 0. If the server has closed the status pipe, the
   write's `EPIPE` is ignored.

**Init** (PID 1):
1. Sets `PR_SET_PDEATHSIG(SIGKILL)`, then waits for "go". EOF means the helper died before
   the death signal was sure to be armed, so init exits (N7 step 1; `getppid()` is 0 for
   PID 1).
2. Resets SIGINT to `SIG_DFL`, so the kernel's PID-1 rule ignores a guest's `kill -INT 1`
   (§1).
3. `setsid()`, so neither init nor the guest has a controlling terminal (§1; second review,
   finding 1).
4. Unshares `NEWNS|NEWNET|NEWUTS|NEWIPC|NEWCGROUP` (A1).
5. Makes `/` `rprivate`, then builds the root. Every mount's flags are in §3.
6. Brings `lo` up (S3-D3).
7. `pivot_root` into the tree, detaches the old root, `chdir("/")`, and sets the hostname to
   `vestibule`.
8. Puts `/dev/null` on fd 0, the guest's pipes on fds 1 and 2, and the socket on fd 3, then
   closes everything from fd 4 up.
9. Drops every capability (bounding set, ambient, `capset`). Then `PR_SET_DUMPABLE 0` and
   `no_new_privs`.
10. Runs its checks (S3-D8). If any fails, it sends an `error` line and exits.
11. Sends `ready` and forks the guest. It reaps every child until the guest exits, then
    reports the guest's exit code or signal and exits.

**The guest:**
1. `close_range(3, ~0)`.
2. Resets SIGPIPE and SIGXFSZ to `SIG_DFL` (§1), as `subprocess`'s `restore_signals` does.
3. `chdir("/workspace")`.
4. Its own PATH search with `execve`. If every entry fails, it writes one line to stderr
   (`vestibule: cannot start python: <reason>`) and exits 127.

**Stopping a run** (timeout, output overflow, a cancelled request):
- The server sends **SIGTERM to the helper,** whose handler SIGKILLs init.
- The kernel then kills everything else in the PID namespace, and reports init's exit only
  once they are all gone. So **the helper's exit means the sandbox is empty** (§1: 3.3–5 ms).
- If the helper hasn't exited 5 s after SIGTERM, the server SIGKILLs it. PDEATHSIG then takes
  init, and the namespace with it.

**The server killed outright:** the PDEATHSIG chain (spike: all gone within 12 ms).
- PDEATHSIG follows the *thread* that forked the child, and asyncio forks on the event-loop
  thread, which lives as long as the server.
- Init's `setsid()` takes init and the guest out of the server's process group, so a client's
  `killpg` reaches only the helper. PDEATHSIG still takes them from there.

**Rejected:** SIGKILLing the helper first, as N8's text has it for the case without cgroups.
The chain from there is asynchronous, so the server couldn't tell when the sandbox is gone,
and would release its concurrency slot too early. It stays as the fallback.

### S3-D8 (fixed; settles N5's open label) — The status report and the per-run verdict

**The status pipe** carries JSON lines. Only the helper holds it; init sends its lines over the
socketpair, and the helper relays them.
- **`ready`** carries init's checks and the facts the server checks itself (below). It is sent
  only after every one of init's checks passed, just before the guest is forked.
- **`error`** carries the stage and a message. It is sent only before the guest exists; the
  guest is then never forked (N5's "abort before `execve`").
- **`exit`** carries `{"code": n}` or `{"signal": s}`. It is decoded on the Linux side, so the
  server needs no wait-status macros, which Windows Python lacks (second review, finding 4).

**What init checks and aborts on** (N5: before the guest exists):

| Check | Passes when |
|---|---|
| `mount_flags` | `statvfs` shows `/` and `/sandbox` `ro,nosuid,nodev`; `/workspace` `nosuid,nodev` (plus `ro` in RO mode); `/tmp` and `/dev/shm` `nosuid,nodev` |
| `caps` | `CapInh`, `CapPrm`, `CapEff`, `CapBnd` and `CapAmb` are all 0 |
| `no_new_privs` | `NoNewPrivs: 1` |
| `not_dumpable` | `PR_GET_DUMPABLE` returns 0 |
| `only_lo` | `/proc/net/dev` lists `lo` and nothing else |
| `no_tty` | `/proc/self/stat`'s `tty_nr` is 0 |
| `seccomp` | step 5 |

The namespaces and the identity map don't need a check of their own in init. If an `unshare`
or a map write fails, setup stops with an `error` at that stage.

**What the server checks itself,** from facts in `ready`, against its own values. This applies
the "don't grade your own homework" rule from the first review: init would otherwise compare
what it built with what its own code meant to build.

| Fact sent | The server requires |
|---|---|
| init's seven namespace inodes | each differs from the server's own (the host's) |
| `uid_map`, `gid_map`, `setgroups` | `<uid> <uid> 1` and `<gid> <gid> 1` with the server's own ids; `deny` |
| the mount table from `/proc/self/mountinfo`: mount point, fstype, per-mount options, super options | the rule below |

**The mount-table rule** (second review, finding 3):
- **Order:** the mount points and fstypes are §3's table, in order. The `/proc` masks are a
  fixed list of candidates: one this kernel lacks may be absent, but an extra or reordered
  entry fails.
- **Flags:** each mount's options must include the flags §3 requires.
- **Forbidden:** `rw` on `/`, on `/sandbox` or on a read-only `/proc` path; a missing `nosuid`
  or `nodev` where §3 has it.
- **Allowed extras:** atime flags (`relatime`, `noatime`, …); a carried `noexec` or `ro`
  (S3-D6); the host's own options on the bound `/dev` nodes.
- **tmpfs sizes:** the soft control `tmpfs-size` is read from the super options' `size=`: for
  `/tmp`, `tmpfs_mb`; for `/dev/shm`, 64 MiB.

**The verdict** is a pure function in the server. The hard set is the server's own constant:
- init's six checks, plus `seccomp`;
- the namespaces, the identity map and the mount table.

It never trusts a list the helper sends.
- An `error` line → `none`: "native sandbox setup failed at <stage>: <message>; nothing was
  executed".
- Neither `ready` nor `error` → `none`: "the sandbox sent no status report; no isolation is
  claimed for this run".
  - This is **N5's open label.** It's `none` because nothing confirms what applied.
  - The wording doesn't claim that nothing ran: a helper killed between relaying init's
    `ready` and writing it would leave a guest that ran.
- A hard check missing, false, or failing the server's own comparison → `none`: "hard controls
  not confirmed: <names>".
- Every hard check passed:
  - every soft control applied → `native`;
  - otherwise → `native-degraded (limits not applied: …)`. The cgroup limits become soft
    checks in step 4.

**In this step, every run reports `none`.** No filter exists yet, so `seccomp` is never
confirmed. Steps 4 and 5 make `native` possible.

**Rejected:** `native-degraded (unverified)` for a run with no report. It claims the hard
tier, which nothing confirmed.

### S3-D9 (fixed; contract §10 step 3) — The test-only seccomp bypass

- **`NativeBackend(_skip_seccomp_for_tests=True)`** skips init's seccomp stage.
- **Without it** (the default), init stops at that stage with "the seccomp filter arrives in
  M2 step 5", so the run reports `none` and nothing is executed. A test proves this.
- No config value or selector path can reach the parameter. Step 6 adds the test that every
  backend the selector builds loads the filter.

### S3-D10 (fixed) — Exit codes and the timeout grace

- **Exit codes, with M1's conventions:**

  | Case | Exit code |
  |---|---|
  | the guest exits with N | N |
  | the guest is killed by signal S | 128+S, Docker's convention |
  | stopped on output overflow | 137, as M1's CLI reports after its kill |
  | timed out | −1, as M1 |
  | setup failed, or no report | −1 |
  | a `ready` line but no `exit` line | −1, with the stderr note "[the sandbox did not report an exit status]" (M1's wording for the same gap) |

- **Grace:** the server stops the sandbox at `timeout_s + 2 s`, counted from the helper's
  spawn (N9 left this to step 3). M1 needed 5 s for container start.
  - Native's start is measured in §1: ~40 ms warm, and ~190 ms for node with a cold page
    cache. 2 s is 10 times that.
  - A smaller grace would make a trivial program with `timeout_seconds: 1` look hung on a slow
    disk (first review, finding 3). A larger one only lets a runaway loop live longer.
- **The outer deadline** stays `timeout_s + 30`.

### S3-D11 (fixed) — The server side mirrors M1's lifecycle

- **Rootfs:** `rootfs.find()` is called for the language's image (python for python and bash,
  node for node; N2).
  - None (or a non-Linux host) gives a `none` result, "no native rootfs for <ref>; run
    `vestibule-mcp setup`", without taking a slot or spawning anything. Step 6 owns the full
    actionable message.
- **Paths:** the workspace and tree paths are made absolute before they reach the helper,
  whose cwd is `/`.
- **Concurrency:** S4-D1's 5 s bounded wait on the backend's own semaphore, then
  `RunRefusedError`. Step 6 adds the parameter for the selector's shared one (S7-D1).
- **Collection:** stdout and stderr are capped at 2 × `max_output_bytes`, as in M1. On
  overflow the server stops the sandbox and notes it in stderr.
- **Status:** read as JSON lines, keeping at most 64 KiB.
- **Timeout:** the result returns as soon as the sandbox is told to stop, with its verdict from
  the `ready` line already read. Nothing on the result path waits for the helper.
- **Finisher:** a detached task owns what is left of the run.
  - It keeps draining the status pipe to EOF, discarding past the cap, so the helper's last
    write never blocks or fails (second review, finding 5).
  - It waits for the helper's exit (SIGKILL after 5 s), logs the helper's exit status, then
    releases the slot (M1's P2 rule).
  - `drain()` behaves as in M1. *(Build: M1's separate `pending_tasks()` was left out. Only
    `drain()` needs it before step 6's selector.)*
- **Logging:** run start and finish go to stderr, as in M1.

### S3-D12 (fixed) — Tests use the real rootfs, behind a `native` marker

- **The `native` marker** (contract §7) skips with its reason when:
  - the host isn't Linux;
  - user namespaces don't work (checked by unsharing one in a child process);
  - or the default `~/.vestibule/rootfs` lacks a pinned tree ("run `vestibule-mcp setup`
    first").
- **Nothing downloads.** The Linux gate allows no skips, so it fails loudly if the WSL cache is
  missing, as it does for missing Docker images.
- **Why not step 2's session download:** it would add network to every native test, and the
  cache is already a gate prerequisite.

### S3-D13 (fixed) — Linux-only code under both platforms' mypy

The Windows gate runs `mypy src` on win32, which has no `memfd_create`, `pidfd_open`, `pipe2`,
`SIGKILL` or `ST_NOEXEC` (first review, finding 1). Step 2's module-level guard can't serve
`native.py`, because §4.1's tests import it on every platform. So:
- **`native_helper.py`** starts with a module-level `if sys.platform != "linux": raise
  ImportError(...)`, as `unpack.py` does. It is only exec'd, or imported by Linux-only tests.
- **`native.py`** imports everywhere. Each function that touches a Linux-only API starts with
  `if sys.platform != "linux": raise RuntimeError(...)`. A function-local `if … raise` makes
  the rest unreachable for Windows mypy; a function-local `assert` does not. Both were checked
  with this repo's mypy.
- **The pure functions** (the verdict, the mount-table rule, the exit mapping, the
  environment) use no Linux-only constant, so §4.1 runs them on Windows.
- **`run()` returns the "not Linux" result** before reaching any guarded function.
- Linux mypy (the Linux gate) checks the code that Windows mypy skips.

## 3. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/native_helper.py` | new: the helper program, stdlib only, exec'd from an fd. Holds the helper, init and guest stages (S3-D4–D7) and init's checks (S3-D8) |
| `src/vestibule/backends/native.py` | new: `NativeBackend`. Holds the config, the launch, collection, the stop path, the finisher, status parsing, the verdict and the mount-table rule, the exit mapping and N9's environment |
| `src/vestibule/backends/base.py` | the isolation-value comment: adds `native-degraded`, drops `namespaces-only` (N5) |
| `pyproject.toml` | the `native` pytest marker |
| `tests/nativeutil.py` | new: the skip rule, and a host-wide scan for a token in process command lines |
| `tests/test_native.py` | new: §4.2, Linux, `native` marker |
| `tests/test_native_verdict.py` | new: §4.1, every platform. Split from `test_native.py` during the build, so the module-level `native` marker covers only the sandbox tests |
| `docs/plans/M2/M2-native-warden.md` | amendment blockquotes: N1, N5, N8, N9, §3's chain and §4.2, A4/A5 (the list in the header) |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | the sign-off bullet, the step-3 entry |

**The mounts init makes,** in order. All of them are under the tree before `pivot_root`:

| Mount point | What | Flags |
|---|---|---|
| `/` | the tree, a non-recursive bind (S3-D6), remounted at once | `ro,nosuid,nodev` (+ locked) |
| `/proc` | a new procfs | `nosuid,nodev,noexec` |
| `/dev` | tmpfs, 64 KiB | `nosuid,noexec` |
| `/dev/{null,zero,full,random,urandom,tty}` | binds of the host's nodes | the host's |
| `/dev/pts` | devpts, `newinstance` (`/dev/ptmx` links to `pts/ptmx`) | `nosuid,noexec` |
| `/dev/shm` | tmpfs, 64 MiB | `nosuid,nodev` |
| `/tmp` | tmpfs, `tmpfs_mb` | `nosuid,nodev` |
| `/workspace` | a non-recursive bind of the workspace (S3-D6) | `nosuid,nodev` (+ `ro` in RO mode; + locked) |
| `/sandbox` | tmpfs holding `main.<ext>` (S3-D1) | `ro,nosuid,nodev` |
| `/proc/<masked>` | `/dev/null` bind, or an empty tmpfs (S3-D2) | `ro` for the tmpfs |
| `/proc/<read-only>` | rebind (S3-D2) | `ro,nosuid,nodev,noexec` |

`/dev` also gets the `fd`, `stdin`, `stdout` and `stderr` links (N9).

**Size check** (Karpathy: "if 200 lines could be 50, rewrite it"):
- **Measured** as code lines: not blank, not a comment, not a docstring.
- **Estimate:** about 300 each for `native_helper.py` and `native.py`. The first prototype is
  about 200 code lines without init's checks, the seccomp stub or the platform guard.
  `container.py`'s matching lifecycle code is a guide for `native.py`.
- **Past ~380:** the design goes back to the plan before the step is called done, and the
  result is recorded here (step 2's lesson).

> **Result (build, 2026-10-25):**
>
> | File | Code lines | Estimate |
> |---|---|---|
> | `native_helper.py` | 308 | ~300 |
> | `native.py` | 378 | ~300; look again past ~380 |
>
> - `native.py` reached 383 after the first `critic` round. That round added `_outcome`,
>   `_host_proc` and the stricter mount-table rule.
> - Looking again found one removable piece: `_exit_code` had a single caller, so it was folded
>   into `_outcome`.
> - The rest is the verification S3-D8 requires, so the design stands.
> - A second simplification (merging the two stream readers) was left out. It touches more
>   than a finding's lines, so it would need another review round, for about six lines.

**Build order, each step with its check:**
1. `native_helper.py` and `native.py`'s run path → hello world in python, bash and node
   passes in WSL on 3.14 and 3.11, and `mypy src` is clean on Windows and Linux (S3-D13).
2. The jail (mounts, masks, locked flags) → §4.2's jail tests pass.
3. PID 1 and the guest's view → §4.2's guest-view, terminal and signal tests pass.
4. The stop path and the finisher → §4.2's kill tests pass, with nothing left behind.
5. The verdict and the abort paths → §4.1's verdict table and §4.2's abort tests pass.
6. Mutation checks (§4.3) → each guarding test turns red.
7. Both platform gates are green, with zero skips on Linux, inside the 540 s budget, and the
   time is recorded.
   - **Expected cost:** about 25 s per pytest run that includes `test_native.py`, and two runs
     do (3.14 full, 3.11 `not docker`). That is roughly 30 short runs at ~0.1 s, six stop
     tests at ~4 s, and the server-kill test.
   - The gate took 143 s after step 2, so about 195 s is expected. Past 250 s, the slow tests
     get looked at before the gate is called green (first review, finding 6).
8. The size check, then docs → `tests/test_docs.py` passes.

## 4. Tests

### 4.1 All platforms, offline (pure functions in `native.py`)

- **The verdict table (S3-D8),** fed status lines and the server's own ids and inodes:
  - `error` → `none` with "nothing was executed";
  - no lines → `none` with "no status report";
  - `ready` without `seccomp` → `none` with "not confirmed: seccomp";
  - a false init check → `none`;
  - an extra key the server doesn't know is ignored, and can't stand in for a missing one;
  - a namespace inode equal to the server's, or a wrong `uid_map` → `none` naming it;
  - **the mount table:**
    - an extra, a missing or a reordered mount → `none` naming the table;
    - `rw` on `/` → `none`;
    - a `/proc` mask this kernel lacks → accepted;
    - a `noatime` workspace, and a carried `noexec` workspace → accepted;
  - a `/tmp` of the wrong size → `native-degraded` naming `tmpfs-size`;
  - everything this step can confirm → `native-degraded (limits not applied: cpu, memory,
    pids)`. *(Build correction: the draft said `native`, but no run has cgroup limits before
    step 4, and N5 claims `native` only when every control holds. Step 4 adds the `native`
    row.)*
  - more than 64 KiB, or no complete line → treated as no report.
- **Exit codes (S3-D10):**
  - `{"code": 0}` → 0, and `{"code": 3}` → 3;
  - `{"signal": 9}` → 137;
  - overflow → 137;
  - no `exit` line → −1 with the note.
- **The guest environment (N9):** the marker's `Env`, plus `HOME`, `TMPDIR`,
  `PYTHONDONTWRITEBYTECODE`, `NODE_OPTIONS` and `HOSTNAME`, with ours winning on a clash.
  Nothing comes from the server's environment: a canary variable set in the test is absent.
- **No rootfs, or not Linux** → `none` with the setup hint, and nothing spawned (the launch is
  replaced by one that fails the test).
- **Busy:** with no free slot, `RunRefusedError` after the bounded wait (the wait shortened by
  monkeypatch, as in `test_lifecycle.py`).

### 4.2 Linux, `native` marker (the real trees)

Each test uses a `tmp_path` workspace. Runs use `_skip_seccomp_for_tests=True` unless they
test the default.
- **Hello world:** python, bash and node print and exit 0. The result is `isolation: none`,
  and the detail names `seccomp` (the step-3 label).
- **Exit codes:** `exit 3` → 3; a guest that SIGKILLs itself → 137.
- **The jail** (parts of criteria 2 and 7):
  - **The canary** is a file at a unique absolute host path,
    `$HOME/.vestibule-canary-<random>/id_ed25519`, removed afterwards. It is unreachable by its
    absolute path, through `/proc/1/root` and `/proc/1/cwd` (both `EACCES`), and by `find /`.
  - **`/proc/self/mountinfo`'s mount points and fstypes** equal §3's list exactly, masks
    included. `/proc` lists only sandbox PIDs, and `/sys` is empty.
  - **Writes:** `/` and `/sandbox` give `EROFS`. `/tmp` fills to `tmpfs_mb` (set to 4 here),
    then `ENOSPC`. `/dev/shm` takes a write.
  - **The workspace:** a round trip works. In RO mode a read works and a write gives `EROFS`.
  - **The masks:** `/proc/keys` reads empty, and nothing under `/proc/sys` is writable.
- **Locked flags and non-recursive binds (S3-D6),** each run under `unshare -r -m` so that the
  mount is locked, as a host's own is:
  - a workspace on a `noexec` tmpfs, and one on a read-only bind: each guest reads the
    workspace, and its `mountinfo` shows the carried flag;
  - a workspace with a tmpfs mounted inside it: `none`, the workspace message, and nothing
    executed.
- **The guest's view** (parts of criterion 10):
  - uid and gid are the host's; groups are as N6 documents;
  - `Cap{Inh,Prm,Eff,Bnd,Amb}` are all 0, and `NoNewPrivs` is 1;
  - the environment is exactly N9's set;
  - the fd table is `{0,1,2}` (the listing's own fd discounted, per step 0's note);
  - `/proc/1/{environ,fd,cwd,root,mem}` are denied. `/proc/1/cmdline` holds no `/home`, no
    username and no `sys.prefix`;
  - hostname `vestibule`, cwd `/workspace`, stdin `/dev/null`;
  - only `lo` exists and it is up: TCP to `127.0.0.1` works, while TCP to an IP, DNS and a UDP
    send fail.
- **No controlling terminal (S3-D7):** the backend runs in a child process under a pty
  (util-linux `script`). The test first checks that this child has a terminal of its own
  (`tty_nr` ≠ 0), so it can't pass vacuously. The guest must then report `tty_nr 0`, and
  `open("/dev/tty")` must fail with `ENXIO`.
- **Signals (S3-D7):** `yes | head -1` prints no `Broken pipe`. After `kill -INT 1; kill -TERM 1`,
  the guest goes on and prints.
- **The stop path** (criterion 8, without cgroups). Each guest starts a background child named
  by a random token (`exec -a <token> sleep 600`):
  - a busy loop, and a self-`SIGSTOP`ped guest, both with `timeout_s=1`: each returns
    `[timed out]` within `timeout_s + 3 s` of the call (the 2 s grace plus margin);
  - a guest that closes stdout and stderr, then lingers: it times out the same way;
  - an output flood: the output is truncated, the stderr note says the sandbox was terminated,
    the exit code is 137, and the run ends early;
  - in every case, after `drain()`, no process holds the token and the helper's PID is gone;
  - and the helper exited **by itself** (status 0, which the finisher logs), not through the
    fallback SIGKILL. That is what makes its exit mean "the sandbox is empty" (S3-D7).
- **The server killed** (criterion 11): a child Python process runs a long token guest through
  `NativeBackend`. The test SIGKILLs it mid-run; no token process remains within 2 s.
- **Abort before `execve`:**
  - the default backend (no bypass) → `none`, "nothing was executed", stage `seccomp`, and the
    guest's marker file is absent from the workspace;
  - a rootfs directory with a valid marker and an empty tree → `none`, "setup failed at mounts";
  - a stand-in helper that prints and exits → `none` with "no status report". Its output goes
    to the server's log, not to the result.
- **Concurrency:** 4 simultaneous runs succeed, with 4 distinct PID-namespace inodes
  (`/proc/self/ns/pid` inside each).
- **Both interpreters:** the gate runs this file on 3.14 and on 3.11 (S3-D5).

### 4.3 Mutation checks (during the build; not committed)

Each mutant must turn its guarding test red:
- no `PR_SET_DUMPABLE 0` → the `/proc/1/environ` test;
- no `setsid()` → the terminal test;
- no SIGPIPE reset → the `yes | head` test;
- no locked-flag carry → the `noexec` workspace test;
- a recursive bind → the "workspace with a mount inside it" test;
- no masks → the `/proc/keys` test;
- a verdict that trusts the helper's list of checks → the extra-key row of the verdict table;
- a mount-table rule that wants exact flags → the `noatime` row;
- a stop that SIGKILLs the helper first → the "helper exited by itself" check.
  - The first review found that a survivor count can't catch this mutant: on this fast host
    the PDEATHSIG chain empties the namespace before the helper is reaped (0 of 15 tries left
    a survivor).
  - The helper's exit status can catch it: 0 when it stopped init itself, −9 when it was
    killed. That needs the finisher to keep the status pipe open (S3-D11), or the correct
    helper would fail its last write.

### 4.4 Done when

- **Gates:** both platform gates are green. On Linux, zero skips, inside the 540 s budget.
- **Size:** the check in §3, with any overrun recorded.
- **Review:** `critic` and `verifier` leave no open CONFIRMED high or medium findings.
- **Docs:** the contract amendments, a HISTORY bullet and a BUILD_LOG entry.

## 5. Edge cases

- **The tree replaced while a run uses it:** `setup` replaces only trees without a valid marker,
  which in practice means after a marker-format change. A run in flight would see files vanish.
  This is rare, and not guarded.
- **A workspace on a read-only or `noexec` filesystem:** carried into the sandbox (S3-D6).
- **A workspace with another filesystem mounted inside it:** refused with its own message
  (S3-D6). The container backend still accepts it.
- **A workspace path that is a symlink:** the bind follows it, as Docker's does.
- **A relative `VESTIBULE_WORKSPACE`:** made absolute against the server's cwd, as
  `read_workspace` and M1 resolve it (S3-D11).
- **The rootfs on a `noexec` mount:** the guest's interpreter can't be exec'd, so it exits 127
  with `cannot start python: Permission denied`. The verdict stays honest, because the sandbox
  was built. Step 6's probe then fails, so native isn't selected.
- **The server inside a container, or under systemd's `ProtectProc=`/`ProtectKernelTunables=`:**
  the host's `/proc` isn't fully visible, so the new procfs mount gets `EPERM` at the mounts
  stage. The run reports `none`, and step 6's message must name the cause (second review,
  finding 12).
- **The username is visible to the guest.** `/proc/self/mountinfo` shows each bind mount's
  source path inside its filesystem: the tree under the user's home, and the workspace's path.
  Docker's bind mounts show the same.
  - Criterion 10's `cmdline` check covers that one channel only. It doesn't make the username
    secret.
  - This is accepted, and it is noted for M4's `SECURITY.md` (second review, finding 7).
- **The server's interpreter can't start the helper** (an embedded build, a missing stdlib):
  there is no status, so the run reports `none`, and the interpreter's own message lands in the
  server's log. Step 6's probe fails.
- **A large `VESTIBULE_MAX_CODE_BYTES`:** the code travels in the memfd and the `/sandbox`
  tmpfs. Neither needs a cap, because the tmpfs is read-only once written.
- **A kernel older than 5.9:** init fails at the fds stage, so the run reports `none`.
- **Nothing limits the guest's resources until step 4.** Native is unreachable until step 6.

## 6. Carried forward

- **Step 4:**
  - "go" is sent only after cgroup placement;
  - the stop becomes `cgroup.kill` on `guest/`, and the helper reads `memory.events` and
    `memory.peak` before it exits (spike A2);
  - `cpu`, `memory` and `pids` join the soft checks;
  - the `RLIMIT_NPROC` backstop.
- **Step 5:**
  - the seccomp stage replaces the stub, and the bypass's default becomes a real filter;
  - `AF_VSOCK`, and the notification listener.
- **Step 6:**
  - a native `cleanup_snapshot()` has nothing to hand off (S3-D1);
  - the shared-semaphore parameter, which the selector passes;
  - the probe is a real run;
  - the overlap check;
  - the messages, including a `/proc` that isn't fully visible (§5).
- **Step 7:** the docs, and the username note for M4's `SECURITY.md` (§5).

## 7. Review record

### First review (draft v1)

`skeptic` reviewed draft v1 on 2026-10-25. Its verdict: **"sound with amendments."**
- It re-ran the §1 prototype in WSL and ran four experiments of its own.
- It confirmed the cooperative stop empties the sandbox before the helper is reaped: 0 of 15
  runs left a survivor, gone within 4–7 ms.
- It confirmed that a helper-stage failure yields no status and so `none`.
- It confirmed that `namespaces-only` appears only in comments and docs.
- It confirmed that `VESTIBULE_BACKEND=native` stays a refusal until step 6.

The main session re-measured three points before amending: the two-part `PYTHONHOME`,
non-recursive binds, and a cold start. It also checked finding 1's mypy claim with the repo's
Windows mypy: a function-local `if sys.platform != "linux": raise` leaves no errors, while a
function-local `assert` reports `Module has no attribute "memfd_create"`.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | med | No design for keeping `native.py` clean under the Windows gate's `mypy src` | Accepted: S3-D13; build-order check 1 |
| 2 | low-med | The `mounts` check compares init's mounts with a list built from the same code | Accepted: the server checks the reported mount table (S3-D8). The re-measurement found recursive binds bring mounts in without `nosuid,nodev`, so binds are non-recursive (S3-D6) |
| 3 | low | A 1 s grace could make a cold node start look like a hang | Accepted: measured cold (~190 ms); the grace is 2 s (S3-D10) |
| 4 | low | The prototype's fixed 16 MiB config read | Accepted: read to EOF (S3-D4) |
| 5 | low | The two-part `PYTHONHOME` form was untested | Accepted: re-run on all three interpreters, clean (§1) |
| 6 | low | The gate's 540 s budget isn't estimated | Accepted: estimate and a 250 s look-again line (§3) |
| — | note | The "kill the helper first" mutant can't be caught by counting survivors | Accepted: the stop tests check that the helper exited by itself (§4.2, §4.3) |

### Second review (draft v2, with the Karpathy check)

After `skeptic` gained its Karpathy check, Victor asked for a second pass before the build.
Its verdict: **"sound with amendments."**
- It reproduced finding 1 in WSL: the prototype guest opened `/dev/tty` under a pty, and
  Docker's didn't.
- It checked finding 4 in the Windows venv: `os.WIFSIGNALED`, `os.WTERMSIG` and
  `signal.SIGKILL` don't exist there.

The main session then ran the v3 design as a second prototype (`proto2_*`, §1) on both
interpreters: no terminal under a pty, the guest pipes as extra fds, the tree read-only at
once, `pidfd_send_signal`, and tmpfs sizes in `mountinfo`. All of it worked.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | The guest inherits the server's controlling terminal and can open `/dev/tty` read-write | Accepted: `setsid()` in init, a `no_tty` hard check, and a pty test (S3-D7, S3-D8, §4.2) |
| 2 | med | The helper's stderr is the guest's stderr from spawn, so interpreter start-up text reaches the model | Accepted, as proposed: the guest's pipes are extra fds; the helper's stdout is `/dev/null` and its stderr is the server's log (S3-D4) |
| 3 | med | The mount-table comparison rule is unstated; read as exact, it underclaims on `noatime` or carried `noexec` hosts | Accepted: the subset rule, with forbidden flags and allowed extras (S3-D8), and verdict rows (§4.1) |
| 4 | med | A wait status can't be decoded on Windows, so the exit-code tests can't run there | Accepted: the exit is decoded on the Linux side (S3-D8) |
| 5 | low-med | A closed or capped status reader makes the helper's last write fail or block, which breaks the "helper exited by itself" check | Accepted: the finisher drains the status pipe; the helper ignores `EPIPE` (S3-D7, S3-D11) |
| 6 | low | Contract amendments marked "fixed" weren't surfaced | Accepted: listed in the header, under Victor's sign-off |
| 7 | low | The "username" reason for the launch contradicts §5's accepted `mountinfo` paths | Accepted: S3-D4 and §5 now say the username is visible |
| 8 | low | The stop tests' 3 s bound can't hold with a 2 s grace | Accepted: `timeout_s + 3 s` from the call (§4.2) |
| 9 | low | `os.kill(pid)` in the SIGTERM handler can hit a reused PID | Accepted: `pidfd_send_signal` (S3-D7) |
| 10 | low | `tmpfs-size` and `identity_map` are init checking its own writes; the tree is writable until the last remount | Accepted: the server checks the identity map, the namespaces and the tmpfs sizes from reported facts; the tree is read-only at once (S3-D6, S3-D8) |
| 11 | low | The size estimate is unmeasured and low | Accepted: measured in code lines, ~300 each, with a look-again line (§3) |
| 12 | low | A `/proc` that isn't fully visible fails at mounts, unexplained | Accepted: §1 assumption, §5 edge case, step-6 message |
| 13 | low | Exit-code parity gaps with M1 (overflow, a missing exit) | Accepted: 137 on overflow, and M1's note for a missing exit (S3-D10) |
| — | note | §1's non-recursive-bind evidence wasn't on disk | Fixed: the `*_norec.py` scripts are in `~/m2spike/step3/` |

### Build review (`critic` and `verifier`, 2026-10-25)

Two `critic` rounds on the built code, each finding confirmed by `verifier` before anything
changed. Each fix got a test that fails first, and a mutant proving the test can fail.

| Round | Sev. | Finding | Fix |
|---|---|---|---|
| 1 | med | The helper's self-deadline never fired: a timed-out `poll` fell into a blocking `recv`, so only the server's SIGKILL fallback ended a run the server forgot | A timed-out poll goes back to the deadline check; new test with the helper's deadline at 1 s |
| 1 | med | The stand-in-helper test didn't guard where the helper's stdout and stderr go (a helper writing to the MCP channel passed) | `capfd` asserts nothing on the server's stdout, the helper's stderr in the server's log, and nothing in the result |
| 1 | med | The mount-table rule treated every `/proc` mask and read-only path as optional, with no `ro` or fstype check, so a missing `/proc/keys` mask still confirmed the hard tier | A mask is expected exactly when the host's `/proc` has it: directory masks are a read-only tmpfs, file masks a bind of the sandbox's `/dev/null` |
| 1 | low | The "[the sandbox did not report an exit status]" note was untested | The exit code and note moved into the pure `_outcome`, tested on every platform, and end to end by the self-deadline test |
| 2 | med | When the self-deadline fired, the helper wrote a log line to the server's stderr before stopping init. A stalled stderr blocked it; a closed one made it exit without stopping init | No log line; the stop comes first. New test with a full, unread stderr |
| 2 | low | `native.py` passed the ~380 look-again line, unrecorded | `_exit_code` folded into `_outcome`; result recorded in §3 |

Mutation checks: all 14 mutants turn their tests red (§4.3's nine, plus one for each fix above).
