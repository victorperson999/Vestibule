# M2 step 4 — cgroups v2: per-run scope, guest limits, OOM report, fork-bomb backstop

Status: **signed off 2026-10-26 (v3, after the `skeptic` review and a Karpathy re-check, §7);
built the same day (§8).**
- **Victor approved S4-D1 and S4-D2 as recommended** (2026-10-26). Each drops something the
  signed-off contract promised; the reason is in the decision.
- **Contract text this plan amends, under the same sign-off:**
  - N3: the mechanism as built (S4-D3–S4-D6), and spike A2's `memory.peak` (S4-D8);
  - N3's "every limit file back": the envelope isn't read back (S4-D6);
  - N3's `RLIMIT_NPROC` backstop: only on kernel ≥ 5.14, as N3's own condition says (S4-D9);
  - N3's "either way" path (S4-D1);
  - N8: no `cgroup.kill` (S4-D2);
  - §3's chain, and §4's cleanup line (S4-D3, S4-D6).
- The other decisions are marked *fixed*: one defensible answer each, most of them forced by
  a measurement in §1.

Contract: [`M2-native-warden.md`](M2-native-warden.md) N3, N5, N8, §3, §4 and §10 step 4.
Builds on [`M2-step3-warden-core.md`](M2-step3-warden-core.md) (its §6 "carried forward"). One
commit.

**What this step delivers:** every native run gets its own systemd scope with the guest's
memory, pids and cpu limits, read back by the server, and the verdict can say `native` once
step 5's filter exists.
- A per-run transient scope, made over the user bus before the helper may start.
- The guest's limits one level above its cgroup-namespace root, read back every run.
- The OOM-kill note.
- Two outside deadlines: systemd's `RuntimeMaxSec`, next to the helper's own.
- Without systemd: the run still happens, says "limits not applied", and gets the
  `RLIMIT_NPROC` fork-bomb backstop.

**Not reachable from the server yet** (contract §10 step 3): only tests build the backend.

**Not in this step** (Karpathy "simplicity first"; each is a later step's job, or nobody's):
- **Step 5:** seccomp. Every real run still reports `isolation: none` until then (S3-D8); one
  test sets the filter's check aside to show what step 4 can claim (S4-D10).
- **Step 6:** selection, the probe, and the "resource limits unavailable" message (§5 of the
  contract).
- **Step 7:** criterion 9's "helper's CPU inside the envelope" (it needs step 5's notification
  storm).
- **Left out entirely:**
  - `memory.peak`, `cpu.stat` and other usage numbers: resource usage is M3's (contract §1);
  - `memory.oom.group`: Docker doesn't set it either;
  - a direct D-Bus client in place of `busctl` (S4-D3);
  - an "already delegated" path (S4-D1) and `cgroup.kill` (S4-D2).

## 1. What the investigation found (2026-10-25, before any code)

A throwaway prototype of this step ran in WSL against the real step-2 trees: step 3's
`native.py` and `native_helper.py` with the placement, the release, the layout, the move of
init, the OOM read and `RLIMIT_NPROC` added, on the gate's two interpreters. The scripts are kept
in WSL at `~/m2spike/step4/` (`proto_native.py`, `proto_helper.py`, `proto_run.py`). Host:
systemd 259, kernel 6.6, cgroup v2 with `nsdelegate`.

| Question | Result |
|---|---|
| A per-run scope from the asyncio server: `busctl --user call … StartTransientUnit` with `PIDs=[helper]`, `Delegate=yes`, the helper paused on a pipe | Works on 3.14 and uv's 3.11. `busctl` returns in 4–10 ms warm (108 ms on the first call after idle). The helper shows up in the scope 5–55 ms after spawn (median 7 ms over 40 runs). The scope is `…/user@1000.service/app.slice/vestibule-<id>.scope`. |
| What it costs (python `print(1)`, 2 × 20 runs per mode, alternating) | Median 47.6 / 43.0 ms with the scope, 45.8 / 41.3 ms without; p95 62 / 68 against 57 / 51. Placement overlaps the helper's own interpreter start, so it adds about **2 ms** to the median. Step 0's spike measured +21 ms with a synchronous harness. |
| The layout (S4-D5's order) | `supervisor/` (the helper moved in) and `guest/sandbox/`, then each of `+cpu`, `+memory`, `+pids` on its own in the scope's `cgroup.subtree_control`, each followed by its files. On `guest/`: `memory.max 268435456`, `memory.swap.max 0`, `pids.max 128`, `cpu.max 75000 100000`, all read back exactly. The cgroupfs writes took 0.8–1.9 ms. |
| The envelope, as unit properties | `MemoryMax` 320 MiB, `TasksMax` 144 and `CPUQuotaPerSecUSec` 1 s landed on the scope's own `memory.max`, `pids.max` and `cpu.max`. `RuntimeMaxUSec` 40 s. Defaults: `KillMode=control-group`, `KillSignal=SIGTERM`. |
| What init and the guest see | Init's `/proc/self/cgroup`, read before `unshare(NEWCGROUP)`, is the host path of `guest/sandbox`. The guest sees `0::/`. |
| Memory bomb (1 GiB against 256 MiB) | A child that allocates: SIGKILLed, the parent carries on, and `oom_kill 1` is in `guest/memory.events` (read by the helper after init's exit). The guest itself allocating: exit 137. The next run is fine. |
| Fork bomb | With the scope: `EAGAIN` after 126 forks (init + guest + 126 = `pids.max` 128). Without one, `RLIMIT_NPROC=128` set in init: `EAGAIN` after 125, because the helper counts too. |
| CPU (a 3 s busy loop) | CPU time / wall time = 0.749–0.751 under `cpu.max` 75 %; 1.0 without. |
| Step 3's stop, with a scope (timeout 1 s; a self-stopped guest with a background child) | Returned at 3.05 s. Nothing left, helper exit 0, the scope gone at once. |
| The helper SIGKILLed mid-run (the server's fallback) | Everything gone within 3 ms; the scope removed. |
| `RuntimeMaxSec` as the outside deadline (the server's grace and the helper's own deadline pushed to 100 s, `RuntimeMaxSec` 3 s, a self-stopped guest with a background child) | systemd stopped the scope at about 3.2 s. Its SIGTERM reached the helper, whose handler killed init: helper exit 0, nothing left. |
| A limit the kernel rejects (`cpu.max "0 100000"`) | `EINVAL` for that file only. Memory and pids were applied, and the verdict named `cpu`. |
| No bus, or a wrong one | `busctl` fails at once ("Failed to connect to user scope bus via local transport"), and the run goes on without a cgroup. `busctl` missing from `PATH` raises `FileNotFoundError` at the spawn, which must be caught. |
| The MCP SDK's minimal environment (`HOME LOGNAME PATH SHELL TERM USER` only) | Placement works with the derived `XDG_RUNTIME_DIR=/run/user/1000`, whose `bus` is a socket owned by the uid. |
| Four runs at once | Four scopes, four PID namespaces, all exit 0. |
| **Re-measured after the review** (`~/m2spike/step4/verify_review.sh`, output in `verify_review.txt`): a scope that ends badly | With the defaults, a scope whose `RuntimeMaxSec` expired stays loaded as `failed` (result `timeout`), and so does one whose PID died while its job was queued (result `resources`). The user manager then reports `degraded`. With `CollectMode=inactive-or-failed`, both are collected (`not-found`). The prototype's first backstop run had left one such unit; it's cleared. |
| A scope member that ignores SIGTERM, at `RuntimeMaxSec` | With the default `KillSignal`, it was still alive 3 s later, the unit `deactivating` (systemd waits `TimeoutStopSec`, 90 s, before SIGKILL). With `KillSignal=9` it was gone. With both properties in the sandbox's scope, the backstop run ended in 3.06 s with helper exit −9 and nothing left. |
| `busctl` on a bus that accepts and never answers | Still blocked at 8 s with `--timeout=2`. `busctl`'s own timeout doesn't cover connecting, so only the server's own bound does. |

**Assumptions** (Karpathy "think before coding"; each is checked or made loud):
- **cgroup v2 is mounted at `/sys/fs/cgroup`.** A v1 or hybrid host fails placement and runs
  without limits, saying so.
- **A systemd user manager is reachable on the user bus,** and `user@<uid>.service` delegates
  `cpu`, `memory` and `pids` (it does here; contract §11). Anywhere else, placement fails the
  same way, and `RLIMIT_NPROC` is the backstop.
- **systemd accepts `RuntimeMaxUSec` on a scope.** Verified on 259 only. A systemd that rejects
  any property fails the whole call, so the run is degraded and the log says why.
- **`busctl` is on the server's `PATH`.** It ships in the `systemd` package on Debian, Ubuntu,
  Fedora and Arch.
- **Swap accounting is on** (`memory.swap.max` exists). Without it the guest could swap past
  `mem_mb`, so `memory` counts as not applied.
- **A failed placement fails fast,** usually: no bus, no `busctl`, a rejected call. One case
  doesn't: a job systemd accepts and then fails while the helper lives. The poll then waits the
  full 5 s, every run on such a host. It's rare, and step 6's probe will see it (§6).
- **Kernel:** step 3 needs 5.9 (`close_range`) and fails setup below that, at the stage
  concerned. Step 3's "≥ 5.14" came from `cgroup.kill`, which S4-D2 drops. Only the
  `RLIMIT_NPROC` backstop needs 5.14 (S4-D9).

## 2. Decisions

### S4-D1 (Victor; amends N3) — No "already inside a delegated subtree" path

N3 says: "if the server is already inside a writable delegated subtree (for example, started by
a unit with `Delegate=yes`), it uses that directly with no D-Bus call."

**Recommendation: leave it out.** Every run uses the per-run scope or runs without limits.

**Why:**
- **It usually needs the server to move.** A cgroup with processes in it can't enable
  controllers for its children (cgroup v2's "no internal processes" rule), and the server is in
  its own cgroup. Using that subtree means moving the server into a leaf below it: N3's rejected
  mechanism (ii), with its side effects, inside the client's cgroup.
  - **The exception:** a unit started with `Delegate=yes` and `DelegateSubgroup=` (systemd 254+)
    keeps its main process in a subgroup, so its own cgroup could hold run cgroups beside it with
    no move. That's from systemd's documentation, untested here.
- **No host we test has it.** On this host the server lands in `/init.scope` (root-owned), from
  `wsl.exe` and from a login shell alike. The path would ship untested.
- **It's an addition, not a dependency.** Nothing else in M2 needs it, and it can come back,
  with its own test host, if a real user needs it.

**What leaving it out costs:** sandboxes run in the user manager's `app.slice`, outside the
client's own cgroup. A `MemoryMax=` a user put on their agent doesn't count the sandbox. Each
sandbox has its own limits instead. N3 already accepted this for mechanism (i).

**The alternative:** build it as N3 says, with a test host started under `systemd-run --user
-p Delegate=yes`. That's roughly 30 lines plus the move, and it's the only option that keeps
sandboxes inside the client's cgroup.

### S4-D2 (Victor; amends N8) — Step 3's stop stays the only stop; no `cgroup.kill`

N8 says: "Timeout or output overflow: the server writes `1` to the run cgroup's `cgroup.kill`.
`SIGSTOP` can't block it."

**Recommendation: keep step 3's stop for every run,** with or without a cgroup: SIGTERM to the
helper, whose handler SIGKILLs init through its pidfd; SIGKILL to the helper after 5 s as the
fallback (S3-D7).

**Why:**
- **It already does what N8 wanted from `cgroup.kill`.** Killing PID 1 of a PID namespace kills
  everything in it, stopped processes included. §1 measured it with a scope in place: a
  self-stopped guest and its background child were gone in milliseconds.
- **It has one signal the server can trust.** The kernel reports init's exit only once the
  namespace is empty, so the helper's exit means the sandbox is gone (S3-D7). `cgroup.kill`
  is asynchronous; the server would still wait for the helper.
- **One path, not two.** With `cgroup.kill`, runs with a cgroup and runs without one would stop
  differently, and each path needs its own tests. Step 3's path is tested and mutation-checked
  (14 mutants).
- **The guest can't reach the helper.** It's outside the guest's PID namespace, so no guest can
  stop the helper from answering a SIGTERM.

**What still sits outside the server:** systemd's `RuntimeMaxSec` (S4-D6) and the helper's own
deadline. Contract §9 criterion 8 ("nothing survives: the run cgroup is gone") is still tested.

**The alternative:** N8 as written. The stop becomes `cgroup.kill` on `guest/` when there's a
cgroup, and the helper's handler otherwise.

### S4-D3 (fixed; implements N3 (i)) — Placement: a per-run scope over `busctl`, the helper paused until it's done

**The helper starts paused.** Step 3's helper starts building at once. Now:
1. It sets `PR_SET_PDEATHSIG` and checks `getppid()`, as before.
2. It reads its **release** from a new pipe to EOF: one JSON object, `{"cgroup": <dir> | null,
   "nproc": <n> | null}`. EOF with nothing means the server gave up, so it exits.
3. Only then does it unshare anything.

**The server, between spawning the helper and starting the timeout clock:**
1. Finds the bus (S4-D4).
2. Runs `busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1
   org.freedesktop.systemd1.Manager StartTransientUnit "ssa(sv)a(sa(sv))"
   vestibule-<run id>.scope fail <properties> 0`, with the properties of S4-D6:
   - stdin from `/dev/null` (the server's stdin is the MCP channel; M0's rule);
   - stdout to `/dev/null` (golden rule 1);
   - stderr captured for the log.
3. Polls `/proc/<helper>/cgroup` every millisecond until it ends in `/vestibule-<run id>.scope`.
   `StartTransientUnit` returns when the job is queued, not when it's done.
4. Builds the layout and writes the limits (S4-D5).
5. Writes the release and closes the pipe.

**Bounds:** the whole placement gets **5 s**, from the `busctl` spawn to the last readback, by
one `asyncio.timeout` around all of it. Normal is 5–60 ms (§1). Past 5 s, or on any failure
(`busctl` missing from `PATH` included), the server kills `busctl` if it's still running and
releases the helper with `"cgroup": null`.
- **Not `busctl --timeout`:** it doesn't cover connecting, and a bus that accepts and never
  answers held `busctl` past 8 s (§1). Without the server's own bound, that run would sit in
  placement until the outer deadline swallowed its result, M1's P2 failure again.
- **The layout's result counts only if it returned inside the bound.** A layout thread still
  running when the bound expires finishes on its own (S4-D5); the release has already said
  `null`, so the verdict never uses it. At worst it leaves an empty directory in a scope
  systemd has already removed.
- **Failure degrades, never blocks.** Limits are the soft tier (N5): the run happens, and the
  verdict says "limits not applied: cpu, memory, pids". One warning line in the log says why.
- **The guest's timeout starts at the release,** so placement never eats the guest's time. The
  outer deadline (`timeout_s + 30`) has room for the 5 s.
- **A late job:** if systemd moves the helper into the scope after the 5 s, the run goes on
  without guest limits. The envelope and `RuntimeMaxSec` then apply to it, and the verdict
  still says the limits weren't applied.

**Where it lives:** a new module, `native_cgroup.py`, with the bus discovery, the placement and
the layout. `native.py` calls it once per run.
- Why not inside `native.py`: it would grow from 378 to about 470 code lines across four
  concerns (lifecycle, verdict, mount rule, systemd). The split follows step 2's precedent
  (`registry.py`, `rootfs.py`, `unpack.py`).
- Each function that touches a Linux-only API starts with S3-D13's `sys.platform` guard, so
  Windows mypy stays clean.

**Rejected:**
- **A direct D-Bus call** instead of `busctl`: about 100 lines of wire protocol (SASL, message
  marshalling) to save the 4–10 ms `busctl` takes. Step 0 called it an optimization, not a
  requirement.
- **`systemd-run --user --scope`** as a wrapper: 96 ms median in step 0, against 75 ms for
  `busctl`.
- **Waiting for systemd's `JobRemoved` signal** instead of polling: it needs a second bus
  connection subscribed before the call, or a bus client of our own. Polling one procfs file
  every millisecond costs nothing measurable.
- **The helper running `busctl` itself:** the server could then only read the limits back while
  the run's status is still arriving. Contract §3 puts placement in the server.

### S4-D4 (fixed; implements N3) — Finding the bus

The environment `busctl` gets is built from scratch, never the server's own (which may hold the
client's tokens):
- `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`, each copied if the server has it;
- if it has neither, `XDG_RUNTIME_DIR=/run/user/<uid>`, but only when `/run/user/<uid>/bus` is
  a socket owned by this uid (`lstat`, so a symlink doesn't count). Otherwise there's no bus,
  and placement fails with "no user bus";
- `PATH`, the server's own (or `os.defpath`), to find `busctl`.

That's N3's rule. §1 checked it under the MCP SDK's six variables.

### S4-D5 (fixed; implements N3's layout) — The layout, the guest's limits, the readback

In order, under the scope:

| Step | What | Why |
|---|---|---|
| 1 | `mkdir supervisor/`; write the helper's PID to `supervisor/cgroup.procs` | The scope must hold no processes before it can enable controllers for its children |
| 2 | `mkdir guest/`, `mkdir guest/sandbox/` | Init goes into `sandbox/`; its cgroup-namespace root is there, so the limit files on `guest/` sit one level above it, out of the guest's reach (contract finding 17) |
| 3 | per controller, `cpu`, `memory`, `pids`: write `+<name>` to the scope's `cgroup.subtree_control`, write its files on `guest/`, read them back | One at a time, so a controller that isn't delegated costs only its own limit |

**The guest's limits** (M1's values and meanings):

| Limit | Files on `guest/` | Value |
|---|---|---|
| `memory` | `memory.max`, `memory.swap.max` | `mem_mb` MiB, and `0` (no swap escape, like M1's `--memory-swap` = `--memory`) |
| `pids` | `pids.max` | `pids_max` (init counts, like M1's `docker-init`) |
| `cpu` | `cpu.max` | `<cpu_pct × 1000> 100000`, what Docker writes for `--cpus` |

- **Applied** means every file of that limit was written and reads back exactly what was
  written. Anything else (a missing file, `EINVAL`, a different value) leaves it out of the
  applied set, with the reason in the log. Writes use `O_WRONLY` without `O_CREAT`.
- **A layout failure** before step 3 (steps 1–2) is a placement failure: `"cgroup": null`.
- **`guest/` gets no `subtree_control` of its own.** `sandbox/` is charged to `guest/`, and
  `guest/memory.events` counts the whole subtree.
- **In a thread:** the layout runs through `asyncio.to_thread`. It's 0.8–1.9 ms here, but moving
  a process between cgroups takes a kernel-wide lock that can take longer on a busy host, and
  the event loop never blocks on namespace or cgroup work (`CLAUDE.md`). The poll in S4-D3 reads
  a procfs file and stays on the loop.

**Init's move:** after forking init, the helper writes init's PID into `guest/sandbox/
cgroup.procs`, then sends init "go". If that write fails, the helper raises: the run reports
`none`, "setup failed", and nothing runs. It's the same uid in the same delegated subtree, so
this isn't a soft limit failing, it's something broken.

### S4-D6 (fixed; implements N3 and N8) — The envelope and `RuntimeMaxSec`, as unit properties

| Property | Value | Why |
|---|---|---|
| `PIDs` | the helper | placement |
| `Delegate` | `true` | the subtree is ours to manage |
| `CollectMode` | `inactive-or-failed` | a scope that ends badly (the backstop fired, or the helper died with the job queued) is removed, not left `failed` with the user manager `degraded` (§1) |
| `RuntimeMaxUSec` | `timeout_s + 30` s | the outer deadline |
| `KillSignal` | `SIGKILL` | the stop at `RuntimeMaxSec` takes every process at once, init included (SIGKILL reaches it from the ancestor namespace). With the default SIGTERM, init ignores it and a broken helper would leave the sandbox for another 90 s (§1) |
| `MemoryMax` | `mem_mb` + 64 MiB | the envelope: the guest's limit plus the helper's allowance |
| `TasksMax` | `pids_max` + 16 | the same |
| `CPUQuotaPerSecUSec` | `cpu_pct` + 25 % of one CPU | the same |

- **The envelope bounds the helper,** and with it the work a guest can provoke there (step 5's
  notifications), without taking it from the guest's own budget (N3). The allowances are step
  0's.
- **The server doesn't read the envelope back** (amends N3's "every limit file"). No label
  claims it. systemd can accept a property whose controller the scope lacks (the review saw it
  with `IOWeight=` and `AllowedCPUs=`). But the envelope's controllers are the guest's: one the
  scope lacks makes that limit's `+<controller>` write fail too, and the verdict names it
  (S4-D5).
- **`RuntimeMaxSec` and the helper's own deadline** sit at the same time, by separate constants
  (one test needs them apart). The helper's deadline works without systemd; systemd's works if
  the helper itself is broken, because `KillSignal=SIGKILL` doesn't need the helper's handler.
  Step 3's first `critic` round found the helper's deadline dead, which is the case this one
  covers.
- **Cleanup:** systemd removes the emptied scope with our sub-cgroups when the helper exits
  (spike A2, and §1), and `CollectMode` removes one that failed. The finisher does nothing new.

### S4-D7 (fixed; extends S3-D8) — The verdict counts only limits the kernel holds for this sandbox

- **Init reports where it is.** Right after "go", before `unshare(NEWCGROUP)`, init reads
  `/proc/self/cgroup` and sends it in `ready` as `cgroup`. It's a kernel fact, like the
  namespace inodes and the mount table, and the server judges it (S3-D8's rule).
- **The server passes the verdict** the `guest/sandbox` path it made (or none) and the set of
  limits it read back.
- **A limit counts as applied** only when init's `cgroup` is exactly `0::<that path>` *and* the
  limit is in the read-back set.
- **Labels:** every hard check, every limit, and `tmpfs-size` → `native`. Otherwise
  `native-degraded (limits not applied: …)`, naming each one, as in step 3.
- **The hard tier is unchanged.** A cgroup problem never makes a run `none`, except a failed
  move of init, which stops the run before the guest exists (S4-D5).

**Every real run still reports `none`** until step 5. The `native` row is reached in §4.1's
pure tests and in one Linux test with the seccomp check set aside (S4-D10).

### S4-D8 (fixed; contract §10 "the OOM-kill report"; amends spike A2) — The OOM note

- **The helper reads `guest/memory.events` after init's exit,** before its own (spike A2:
  systemd removes the scope as soon as it's empty). It sends `{"oom": n, "oom_kill": m}` as its
  own line on the status pipe, after init's lines, ignoring `EPIPE`. A read failure sends
  nothing.
- **What the two counters mean** (the review measured both): `oom` counts the times `guest/`'s
  *own* limit was hit; `oom_kill` counts processes in `guest/` killed by *any* OOM kill,
  including the envelope's and a host-wide one.
- **The server adds a stderr note** when `m` ≥ 1:
  - `memory` applied and `n` ≥ 1: `[out of memory: the kernel killed a process in the sandbox
    at its <mem_mb> MiB limit]`;
  - otherwise: `[out of memory: the kernel killed a process in the sandbox]`. It never names a
    limit that didn't apply or didn't fire.
- The note goes with the other notes in `_outcome`, so the pure tests cover it. It's added after
  a normal exit or a missing one, not on a timeout or an overflow (those return before the
  helper's line exists).
- **Exit codes don't change:** a guest killed by the OOM killer reports 137, as in M1.
- **`memory.peak` isn't read.** Spike A2 listed it, but nothing uses it before M3's usage
  report.
- **M1 parity:** M1 reports the 137 but no note. This note is native-only for now; adding it to
  M1 would be a change to M1, not part of this step.

### S4-D9 (fixed; implements N3's backstop) — `RLIMIT_NPROC` when there's no `pids` limit

- When `pids` isn't applied (no cgroup, or the controller missing) **and the kernel is
  5.14 or later**, the release carries `"nproc": pids_max`. Init sets `RLIMIT_NPROC` to it (soft
  and hard) before forking the guest.
- **What it counts:** every process of the uid in the sandbox's user namespace, including the
  helper. §1's bomb stopped at 125, against 126 with `pids.max`.
- **Why the kernel check** (N3's own condition: "if … counted per user namespace (5.14+)"):
  before 5.14 the kernel counts `RLIMIT_NPROC` per uid across the whole host. A user who
  already owns 200 processes would then have every guest fork fail, and `print(1)` would come
  back as "the sandbox did not report an exit status". Below 5.14 there's no backstop, and the
  label already says `pids` wasn't applied.
- **The check is the server's:** a pure function of `os.uname().release` (major and minor
  numbers), tested on every platform.
- **It's a backstop, not a limit.** The label still says `pids` wasn't applied, because the
  user's process table is what it protects, not the configured `pids_max`.
- **With `pids` applied, it isn't set.** One limit decides, and criterion 5 ("stops at
  `pids.max`") stays true.

### S4-D10 (fixed) — How the tests reach cgroups

- **A `cgroups` skip rule** in `tests/nativeutil.py`, for the tests that need limits: a user bus
  (S4-D4's rule) and `user@<uid>.service` delegating `cpu memory pids`. Otherwise they skip with
  that reason. The Linux gate allows no skips, so it fails if WSL loses its user manager.
- **The degraded paths by injection:**
  - the bus discovery monkeypatched to "none";
  - **a fake `busctl`** first on `PATH` (S4-D4 passes the server's `PATH` through): one that
    sleeps, for the 5 s bound and for a cancel that lands inside placement every time; and a
    `PATH` without `busctl`, for the `FileNotFoundError` path.
- **What step 4 can claim, before step 5:** one test removes `seccomp` from the verdict's
  `_HARD_CHECKS` constant, only inside that test, and expects `native`. Nothing in `src/` gains
  a switch for it (S3-D9 stays the only test-only parameter).
- **Every native test checks its scopes are gone afterwards** (criterion 8): the `backend`
  fixture lists the user manager's `vestibule-*.scope` units, loaded or failed, before and after
  the test. Allowing systemd 100 ms, no unit may be in the second list that wasn't in the first.
  A before/after difference, so a unit something else made can't fail the test.

## 3. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/native_cgroup.py` | new: bus discovery (S4-D4), placement (S4-D3), the layout, limits and readback (S4-D5), the unit properties (S4-D6) |
| `src/vestibule/backends/native.py` | the release pipe; placement between spawn and collection; the verdict's cgroup inputs (S4-D7); the OOM note in `_outcome` (S4-D8); the kernel check for the backstop (S4-D9); `_RUNTIME_MAX_SLACK_S` |
| `src/vestibule/backends/native_helper.py` | the paused start and the release; init's move into `guest/sandbox`; init's `cgroup` in `ready`; `RLIMIT_NPROC`; the `oom_kill` line |
| `tests/nativeutil.py` | the `cgroups` skip rule (S4-D10); a scope listing for the teardown check |
| `tests/test_native.py` | §4.2's new tests; the fixture's scope check; the killed-server test checks the scope too |
| `tests/test_native_verdict.py` | §4.1's new rows |
| `docs/GETTING_STARTED.md` | the native-tests bullet: the cgroup tests need a systemd user manager |
| `docs/plans/M2/M2-native-warden.md` | amendment blockquotes: N3, N8, §3's chain, §4's cleanup, spike A2 |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | the sign-off bullet, the step-4 entry |

**The run, as built** (contract §3, changed lines marked ▲):

```
server: spawn the helper (paused ▲)
        ▲ busctl StartTransientUnit vestibule-<id>.scope (PIDs=[helper], Delegate, CollectMode,
          RuntimeMax, KillSignal=SIGKILL, envelope)
        ▲ poll /proc/<helper>/cgroup until it is in the scope (≤ 5 s for the whole placement)
        ▲ supervisor/ ← helper; guest/sandbox/; per controller: enable, write guest/'s limits, read back
        ▲ release {"cgroup": guest/sandbox | null, "nproc": n | null}; start the timeout clock
helper: PDEATHSIG; ▲ wait for the release; unshare(USER|PID); maps; fork init;
        ▲ init → guest/sandbox/cgroup.procs; "go"; relay; wait; ▲ read guest/memory.events → oom line
init:   ▲ read /proc/self/cgroup; setsid; unshare(NS|NET|UTS|IPC|CGROUP); jail; privileges;
        ▲ RLIMIT_NPROC if "nproc"; checks; ready (▲ with cgroup); guest; reap; exit line
server: collect; verdict (▲ placed + read-back limits); ▲ OOM note; systemd removes the scope
```

**Size check** (Karpathy: "if 200 lines could be 50, rewrite it"), in code lines as step 3
measured them:
- **`native_cgroup.py`:** about 110. The prototype's placement is already about 85, before the
  bound, the `busctl` kill, the `FileNotFoundError` catch, the thread split and the platform
  guards (review finding 10). Look again past ~130.
- **`native.py`:** 378 now, about 420 after. The prototype's `native.py` is 516; less its own
  knobs and timers and the placement that moves to `native_cgroup.py`, it adds about 40 lines
  here (the Karpathy re-check, §7). Look again past ~440.
- **`native_helper.py`:** 308 now, about 325 after. Look again past ~340.
- The result is recorded here, and an overrun is looked at before the step is called done
  (step 2's lesson).

**Build order, each step with its check:**
1. `native_cgroup.py`, the release, and the helper's pause and move → a hello run lands in a
   scope (the outside-readback test, §4.2), on 3.14 and 3.11; `mypy src` clean on both
   platforms.
2. The bound, the `busctl` kill and the `FileNotFoundError` catch → the slow and missing
   `busctl` tests, and the cancel test.
3. The verdict's inputs and init's report → §4.1's rows and the "what step 4 can claim" test.
4. The OOM line and note → §4.1's note rows and the memory-bomb test.
5. `RLIMIT_NPROC` and its kernel check → §4.1's kernel rows and the degraded fork-bomb test.
6. The unit properties → the envelope readback and the `RuntimeMaxSec` test.
7. Mutation checks (§4.3) → each guarding test turns red.
8. Both platform gates green, zero skips on Linux, inside the 540 s budget; the time recorded.
   - **Expected cost:** about 20 s more per pytest run that includes `test_native.py` (the
     slow-`busctl` test ~6 s, the backstop test ~4 s, the CPU test ~3 s, the bombs, the
     per-test scope check), and two runs do. The gate took 174 s after step 3, so about 215 s.
     Past 250 s, the slow tests get looked at first.
9. The size check, then docs → `tests/test_docs.py` passes.

## 4. Tests

### 4.1 All platforms, offline

- **The verdict (S4-D7),** fed `ready` with a `cgroup` field, the expected path and a read-back
  set:
  - every hard check, all three limits read back, init in the expected cgroup, `tmpfs-size`
    right → `native`, no detail;
  - the read-back set without `cpu` → `native-degraded`, "limits not applied: cpu";
  - init in a different cgroup, or no `cgroup` field → all three named;
  - no expected path (placement failed) → all three named (step 3's row, kept);
  - `cpu` missing and a wrong `/tmp` size → "cpu, tmpfs-size";
  - a cgroup problem never turns a hard-tier pass into `none`.
- **The OOM note (S4-D8),** in `_outcome`:
  - `{"oom": 1, "oom_kill": 1}` after an exit line, `memory` applied → the code and the note
    with `mem_mb`;
  - the same with `memory` not applied, or with `oom` 0 (another OOM kill) → the note without a
    limit;
  - `oom_kill` 0, a non-integer, or no line → no note;
  - `ready`, no exit line, and an OOM kill → both notes;
  - on a timeout or an overflow → no OOM note.
- **The backstop's kernel check (S4-D9):** `6.6.87.2-microsoft-standard-WSL2` and `5.14.0` → the
  backstop; `5.10.0-21-amd64` and `4.19.0` → none; a release string it can't read → none.

### 4.2 Linux, `native` marker; the limit tests also need `cgroups`

Each test uses a `tmp_path` workspace and the step-3 fixtures; every one ends with the
fixture's "no scope left" check (S4-D10).
- **The limits, from outside** (parts of criteria 4–6): during a run, the test finds the
  guest's cgroup through `/proc/<guest pid>/cgroup` (the guest is named by a token).
  - It ends in `/vestibule-<id>.scope/guest/sandbox`.
  - `guest/`'s `memory.max`, `memory.swap.max`, `pids.max` and `cpu.max` hold the requested
    values.
  - The scope's own `memory.max`, `pids.max` and `cpu.max` hold the envelope.
  - The helper is the only process in `supervisor/`.
  - Nothing reached the server's stdout (golden rule 1, `capfd` as step 3's helper test does).
    `busctl` prints the job's path there if it inherits stdout.
- **A failed move of init** (S4-D5): placement monkeypatched to release a `guest/sandbox` path
  that doesn't exist, with every limit "applied" → `none` with step 3's setup-failure detail,
  and the guest's token never appears.
- **Memory bomb** (criterion 4): a child allocating 1 GiB is SIGKILLed while the parent
  reports; a guest allocating it itself exits 137; both results carry the OOM note; the next
  run prints.
- **Fork bomb** (criterion 5): `EAGAIN` after `pids_max − 2` forks; the run returns within a
  few seconds; the next run works.
- **CPU** (part of criterion 6): a 2 s busy loop's CPU time / wall time is at most 0.85 with
  `cpu_pct` 75. No lower bound, so a loaded host can't fail it.
- **A rejected limit:** `Limits(cpu_pct=0)` (`cpu.max "0 100000"` is invalid) → with the
  seccomp check set aside, `native-degraded`, "limits not applied: cpu" only.
- **What step 4 can claim** (S4-D10): with `seccomp` taken out of `_HARD_CHECKS`, a hello run
  reports `native` and no detail.
- **No bus** (criterion 13's "no delegation", by injection): with the seccomp check set aside,
  `native-degraded (limits not applied: cpu, memory, pids)`; the log says why; a fork bomb
  stops at `RLIMIT_NPROC` (at most `pids_max` processes).
- **`RuntimeMaxSec`** (S4-D6): with the server's grace and the helper's deadline pushed past
  100 s and `RuntimeMaxSec` set to 3 s, a self-stopped guest with a background child ends
  within 6 s, nothing holds its token, and the helper's exit is −9. That's systemd's SIGKILL:
  the helper couldn't have ended the run itself, and its handler would have exited 0.
- **The minimal environment** (the bus half of criterion 17): a child process with only the MCP
  SDK's six variables runs a memory bomb through `NativeBackend`, and the result carries the
  OOM note. That proves placement found the bus by itself.
- **A `busctl` that hangs** (S4-D3's bound): a fake `busctl` first on `PATH` sleeps 60 s and
  writes its PID to a file. With the seccomp check set aside, the run returns `native-degraded
  (limits not applied: cpu, memory, pids)` within 5 s plus the run; the fake's PID is gone; no
  helper is left.
- **No `busctl` on `PATH`:** the same result, at once.
- **Cancelled during placement:** with the hanging fake, the request is cancelled 0.5 s in, so
  it lands inside placement every time. No helper, no fake `busctl`, no scope is left.
- **The killed server** (step 3's test): no token process and no scope left within 2 s.
- **Both interpreters:** the gate runs this file on 3.14 and 3.11.

### 4.3 Mutation checks (during the build; not committed)

Each mutant must turn its guarding test red:
- the helper doesn't move init → the memory-bomb test (the bomb survives) and the "what step 4
  can claim" test;
- the limits written on `supervisor/` instead of `guest/` → the outside-readback test;
- no `memory.swap.max` → the outside-readback test;
- no `cpu.max` → the CPU test;
- no `pids.max` → the fork-bomb test (the envelope's `TasksMax` stops it later, at about 141);
- no `RLIMIT_NPROC` without a cgroup → the no-bus fork-bomb test (the guest caps its own bomb
  at 4 × `pids_max`, so the mutant can't flood the host);
- the verdict ignores init's reported cgroup → the "a different cgroup" row;
- the verdict counts a limit that didn't read back → the "without `cpu`" row and the
  rejected-limit test;
- no OOM line or note → the memory-bomb test;
- the limit named whenever `oom_kill` ≥ 1 → the "`oom` 0" and "`memory` not applied" rows;
- the backstop on any kernel → the `5.10` row;
- no bound around `busctl` (the prototype's shape) → the hanging-`busctl` test;
- an uncaught `FileNotFoundError` → the no-`busctl` test;
- no `RuntimeMaxUSec` → the `RuntimeMaxSec` test;
- `KillSignal` left at SIGTERM → the `RuntimeMaxSec` test (the helper's exit is 0, not −9);
- no `CollectMode` → the fixture's scope check after the `RuntimeMaxSec` test (the unit stays
  `failed`);
- no envelope properties → the outside-readback test;
- no derived `XDG_RUNTIME_DIR` → the minimal-environment test;
- `busctl`'s stdout inherited → the outside-readback test's stdout check;
- the helper ignores a failed move of init → the failed-move test (the guest runs);
- `busctl` killed only when the bound expires, not on a cancel → the cancel test (the fake's PID
  is still there).

### 4.4 Done when

- **Gates:** both platform gates green. On Linux, zero skips, inside the 540 s budget.
- **Size:** the check in §3, with any overrun recorded.
- **Review:** `critic` and `verifier` leave no open CONFIRMED high or medium findings.
- **Docs:** the contract amendments, the GETTING_STARTED bullet, a HISTORY bullet and a
  BUILD_LOG entry.

## 5. Edge cases

- **A request cancelled during placement:** the finisher SIGTERMs the paused helper (no handler
  yet, so it dies); placement kills `busctl` if it's still running; a scope that was made
  empties and is removed, or fails and is collected (`CollectMode`).
- **systemd slower than 5 s:** the run goes on without limits (S4-D3). If the helper lands in
  the scope later, the envelope and `RuntimeMaxSec` apply to it.
- **A user bus that accepts and never answers:** the 5 s bound ends the wait; `busctl` is
  killed (§1).
- **A job systemd accepts and then fails, with the helper alive:** every run waits the full
  5 s (§1 assumptions). Step 6's probe measures it.
- **A kernel older than 5.14 without systemd:** no limits and no backstop; the label says
  `cpu, memory, pids` weren't applied (S4-D9).
- **The helper dies before placement** (the stand-in helper of step 3's test): the poll ends
  when `/proc/<pid>` is gone, and the release write's `EPIPE` is ignored.
- **A controller not delegated** (some older systemd releases delegate only `memory pids` to
  `user@.service`): its limit is named as not applied; the others hold.
- **No swap accounting:** `memory` is named as not applied (§1 assumptions).
- **The OOM killer picks init:** possible, since Docker's rule (no `oom.group`, largest process
  first) is kept. Init is small, so the bomb goes first in practice. If init dies, the result
  is −1 with the "no exit status" note and the OOM note.
- **`/tmp` and `/dev/shm` count against `memory.max`:** tmpfs pages are charged to the writer's
  cgroup, as in Docker.
- **The server in a cgroup namespace** (a container): `/proc/<pid>/cgroup` paths are relative to
  its namespace root, which is what its `/sys/fs/cgroup` shows, so the paths still line up.
  Such hosts rarely have a user bus.
- **The server killed outright:** the PDEATHSIG chain ends the helper (paused or not), and
  systemd removes the emptied scope. A job still queued for a dead PID fails on systemd's side,
  and `CollectMode` removes that unit too.
- **Sandboxes leave the client's cgroup** (S4-D1): stated in the decision; step 7's docs say it.
- **The stalled-stderr known issue** (BUILD_LOG, every backend): placement logs only on failure,
  and never on a stop path.

## 6. Carried forward

- **Step 5:** the seccomp stage; the first real `native` result; criterion 9's envelope check.
- **Step 6:**
  - the probe reports the placement outcome, and how long it took: a host where placement
    always waits out its 5 s (§1 assumptions) can be told apart there;
  - the "resource limits unavailable" message names the reason the log has (no bus, no
    delegation, a rejected property);
  - criterion 17's full launch under the minimal environment.
- **Step 7:** the README's note that sandboxes run outside the client's cgroup (S4-D1), and the
  benchmark with placement in it.

## 7. Review record

### First review (draft v1, with the Karpathy check)

`skeptic` reviewed draft v1 on 2026-10-26. Its verdict: **"sound with amendments."**
- It ran its own experiments in WSL (`~/m2spike/review4/`): failed scopes, a silent bus,
  property acceptance, OOM counters.
- It found a unit the prototype had left `failed`, with the user manager `degraded`.

Before amending, the main session re-measured the claims the three medium findings rest on
(§1's last three rows, `~/m2spike/step4/verify_review.sh`):
- the failed units, and `CollectMode` collecting them;
- the SIGTERM-ignoring member, and `KillSignal=9` killing it;
- `busctl --timeout` not bounding a silent bus;
- and both new properties in the sandbox's own scope.

All reproduced. The leftover unit was cleared (`systemctl --user reset-failed`).

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | med | A scope that ends badly (the backstop fired, or the helper died with its job queued) stays loaded as `failed` and leaves the user manager `degraded`; the teardown check and the cancel test would fail on it | Accepted: `CollectMode=inactive-or-failed` (S4-D6); the teardown check is a before/after difference (S4-D10); §5 corrected |
| 2 | med | The 5 s placement bound carries the outer-deadline budget, but nothing tests it, and the prototype had none (`busctl --timeout` doesn't cover connecting) | Accepted: one bound around the whole placement (S4-D3); hanging-, missing- and cancel-`busctl` tests with a fake on `PATH` (S4-D10, §4.2); a "no bound" mutant (§4.3) |
| 3 | med | Without `cgroup.kill`, nothing needs 5.14, and before 5.14 `RLIMIT_NPROC` counts the uid's processes across the host, so the backstop could fail every guest fork | Accepted: the backstop only on 5.14+, a pure check (S4-D9); the kernel assumption corrected (§1) |
| 4 | low-med | `oom_kill` counts any OOM kill, so the note could blame a limit that wasn't applied or didn't fire | Accepted: the helper sends `oom` and `oom_kill`; the limit is named only when `memory` applied and `oom` ≥ 1 (S4-D8) |
| 5 | low | "systemd's works if the helper is broken" overclaims: init ignores SIGTERM, so a broken helper leaves the sandbox for `TimeoutStopSec` (90 s) | Accepted: `KillSignal=SIGKILL` (S4-D6); the backstop test expects helper exit −9 (§4.2) |
| 6 | low | The envelope's "can't be silently missing" reason is wrong (systemd accepts properties whose controller is absent), and dropping its readback contradicts N3 unlisted | Accepted: the right reason stated; the N3 amendment listed in the header (S4-D6) |
| 7 | low | A job that fails after it's queued, with the helper alive, costs the full 5 s every run | Accepted: an assumption (§1), an edge case (§5), and step 6's probe (§6) |
| 8 | low | `busctl`'s stdin is unspecified | Accepted: `/dev/null` (S4-D3) |
| 9 | low | A layout thread outlives a bound that expired | Accepted: its result counts only inside the bound (S4-D3) |
| 10 | low | ~70 lines for `native_cgroup.py` is below the prototype itself | Accepted: ~110, look again past ~130 (§3) |
| 11 | low | `DelegateSubgroup=` (systemd 254+) is a delegated subtree that needs no server move | Accepted: stated in S4-D1 as the exception; the recommendation stands, since no tested host has it |

### Karpathy re-check (draft v2, main session, 2026-10-26)

Victor asked whether the plan follows the four rules. The main session checked v2 against
`KARPATHY_EXAMPLES.md` again, and counted the prototype's code lines with step 3's measure.
Three gaps, none of them in the review above:

| # | Rule | Gap | Fix |
|---|---|---|---|
| 1 | Simplicity (size check) | `native.py`'s ~400 was a guess. The prototype implies ~420, right at the look-again line, as in step 3 (383 against ~380) | ~420, look again past ~440, with the count behind it (§3) |
| 2 | Goal-driven | `busctl`'s stdout to `/dev/null` (golden rule 1) had no test and no mutant | a `capfd` check in the outside-readback test, and its mutant (§4.2, §4.3) |
| 3 | Goal-driven | Two stated behaviours had no test that could fail: a failed move of init → `none` (S4-D5), and `busctl` killed on a cancel (§5) | a failed-move test; a mutant for each (§4.2, §4.3) |

### `critic` on the diff (2026-10-26), checked by `verifier`

Eight findings: seven CONFIRMED, one PLAUSIBLE, none refuted. Each code fix came with a test
that failed on the code before it, or, for a test gap, a mutant that survived before it.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | low (critic: med) | Without a `pids` limit, init asked for `RLIMIT_NPROC` = `pids_max` even when the user's hard limit was lower. No process can raise its hard limit, so every run was refused at "privileges" (`ulimit -u 100`) | Fixed: clamped to the hard limit. `test_a_hard_nproc_limit_below_pids_max_still_runs` failed first |
| 2 | med | Nothing tested the readback compare itself: the rejected-limit test fails at the write. `if True:` in its place passed every test | Fixed: `test_a_limit_that_reads_back_differently_is_named` |
| 3 | low | Nothing tested the backstop with a scope but no `pids` controller ("no cgroup, *or the controller missing*", S4-D9) | Fixed: `test_a_controller_not_delegated_is_named_and_the_backstop_holds` (125 forks, against about 141 without it) |
| 4 | low | Nothing tested that `bus_env()` builds from scratch, nor its symlink and owner checks | Fixed: two offline tests, with a canary variable and five socket cases |
| 5 | low (PLAUSIBLE) | On a cgroup-v1 host every run would wait the full 5 s, and the log would blame the bound | Fixed: placement first checks for `cgroup.controllers` at the v2 root. `test_without_cgroup_v2_placement_fails_at_once` failed first, logging "placement took over 5 s" |
| 6 | low | `native_cgroup.py` past its look-again line, unrecorded | Looked at: `_start_unit` folded; the rest recorded in §8 |
| 7 | low | The scope check polls 1 s, against S4-D10's 100 ms | Kept, recorded in §8 |
| 8 | low | This status line was stale; BUILD_LOG had no entry yet | Fixed (the BUILD_LOG entry is the docs step) |

## 8. As built (2026-10-26)

- **Sizes** (code lines, step 3's measure):
  - `native_cgroup.py`: 136, against ~110 (look again past ~130). Looked at twice. The five
    ways placement can fail each log their own reason (S4-D3, S4-D5), and the v2 check from
    the review added three lines. `_start_unit` was folded. Nothing else could go without
    losing a stated reason.
  - `native.py`: 428 (estimate ~420, look again past ~440).
  - `native_helper.py`: 329 (estimate ~325, look again past ~340).
- **Departures from the plan's wording, all in the tests:**
  - **The fake `busctl`** is injected by monkeypatching `bus_env()` to a `PATH` that holds it,
    not through the server's own `PATH`. The tests then don't depend on the host's bus. The
    real runs cover `PATH` passing through.
  - **The scope check** is an autouse fixture for every test in `test_native.py`, so the
    subprocess-based tests (the killed server among them) are checked too. It polls for up to
    1 s, not 100 ms. A unit the change leaves behind (failed, or never emptied) stays for
    good, and the `CollectMode` mutant turns it red; a fixed 100 ms would only add flakiness
    on a loaded host.
  - **The failed-move test** checks that a `ran` file was never written, as step 3's refusal
    test does, not a guest token.
- **Mutation checks:** 21 from §4.3 and 7 from the `critic` round, all red. Two of them
  hang rather than fail an assertion: no bound around `busctl`, and no `RuntimeMaxUSec`.
  The harness's 75 s limit and the gate's budget catch both.
- **Gate:** green on both platforms. On Linux: 470 tests, 0 skipped, 207 s, against the ~215 s
  expected (look again at 250 s). On Windows: 367 passed, 83 Linux- or POSIX-only skips.
