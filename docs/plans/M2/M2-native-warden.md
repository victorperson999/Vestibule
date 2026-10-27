# M2 contract — Native Linux warden

Status: **signed off by Victor 2026-10-14** (v2, revised after the `skeptic` review, §12).
- **Signed off:** E1–E3; the three choices the review raised (in N4 and N5): seccomp is a
  hard control, the filter is an allowlist derived from Docker's, and Ubuntu's
  user-namespace restriction is tested by injection in M2 and on a real VM in M4; and N1–N5
  as a whole.
- **Step 0 done 2026-10-14:** [`M2-step0-spike.md`](M2-step0-spike.md). No decision reversed;
  the corrections are recorded below as amendments.
- **Step 0 (a spike) tests the kernel facts N1, N3, N4 and N7 rest on.** If it contradicts
  a signed-off decision, that decision comes back to Victor before any step builds on it.
- **Inputs:** `docs/PLAN.md` §Milestone 2, `docs/ARCHITECTURE.md` Part 2, `CLAUDE.md`, the M1
  contract (`docs/plans/M1/M1-container-backend.md`), the `docs/BUILD_LOG.md` known issues
  owned by M2, and the dev-host facts in §11.

---

## 1. Scope

**In:**
- **`NativeBackend`:** one sandbox per `run_code` call, without root and without Docker. It
  is built from:
  - Linux user, mount, PID, network, UTS, IPC and cgroup namespaces
  - a `pivot_root` filesystem jail
  - cgroup v2 limits
  - `no_new_privs` and an empty capability set
  - a seccomp allowlist
- **A rootfs:** the same digest-pinned `python` and `node` images M1 uses, fetched once by an
  explicit setup command and unpacked on disk (N2).
- **Backend selection on Linux:** native first, M1's container backend as the fallback, with
  an honest `isolation:` value for every outcome (N5).
- **Denied-syscall reporting:** `RunResult.denied_syscalls` already exists, and
  `_format_result` renders it as `blocked syscalls:`.
- **A cold-start benchmark:** native vs container, on the same host.
- **A Linux baseline:**
  - the whole existing suite runs on Linux for the first time, container backend included
  - the POSIX `SIGTERM` handler from the BUILD_LOG known issues

**Out (explicitly not M2):**
- Audit log, and resource usage in results as a feature (M3). M2 reads `memory.events`,
  `memory.peak` and `cpu.stat` only to verify limits or report an OOM kill.
- Native isolation on macOS or Windows. Those hosts keep the container backend, unchanged.
- The Windows→WSL bridge (Windows Claude Code launching the server through `wsl.exe`). It's
  an M4 adoption candidate (E2).
- Network policies beyond "no network", warm sandbox pools, GPUs, custom images.
- A structural fix for guest-planted symlinks (N4 explains why).
- An AppArmor profile for Ubuntu's restricted user namespaces. M2 does detection, fallback
  and a README availability table only; a stock-Ubuntu VM run is in M4 (N5).

---

## 2. Decisions

### Environment — signed off by Victor 2026-10-14

- **E1 — One copy of the code: the Windows repo. Linux tests run in WSL.**
  - Claude edits the Windows checkout, and Victor commits from Windows as before.
  - The Linux side is a venv kept outside the repo, `~/.venvs/vestibule` in WSL Ubuntu, with
    an editable install of `/mnt/c/Users/victo/Documents/projects/vestibule`.
  - Test temp dirs, the workspace and the rootfs cache all live on WSL's own ext4 disk, so
    isolation tests see real Linux file behaviour.
  - Only the source sits on the slower `/mnt/c` mount (§11). The benchmark is the exception:
    it runs from a wheel installed on ext4 (§9 criterion 14).
- **E2 — The live-agent check uses Linux Claude Code inside WSL.** That's what a Linux user
  runs, so it's the honest acceptance target. Installing it is a step-7 task; the `claude`
  WSL finds today is the Windows binary.
- **E3 — Plan shape like M1.** This contract and its step list now, then a detailed plan
  for each step just before it's built, reviewed by `skeptic` and signed off by Victor.

### N1 (signed off 2026-10-14) — The warden is a fresh helper process per run, not a fork of the server

For each run the server starts a new, single-threaded Python interpreter, the **warden
helper**. It uses `asyncio.create_subprocess_exec` with `-I` (isolated mode) and a **minimal
environment**: nothing from the server's environment, which may hold the client's tokens or
proxy settings.
- The helper does every namespace and mount step, then supervises the guest.
- The server never forks itself into a sandbox and never makes a blocking namespace
  syscall.

- **Why:**
  - The server is multi-threaded: the event loop, executor threads and the MCP SDK's anyio
    machinery. `fork()` copies only the calling thread, so any lock another thread held
    (logging, the import lock, allocator arenas) stays locked forever in the child. Python
    3.12+ warns about exactly this.
  - Golden rule 3 becomes structural: the long-lived process is never part of a sandbox.
  - The server side reuses M1's proven async shape: spawn, stream with caps, time out, clean
    up.
- **Cost:** measured by the review on this host, a one-file stdlib helper starts in **17 ms**
  from ext4 (22 ms from `/mnt/c`; the bare interpreter takes 9 ms). Several Docker CLI calls
  at ~130–145 ms each make up a container run here.
- **Rejected:**
  - Forking from an executor thread. That's what `CLAUDE.md` and `ARCHITECTURE.md` say today,
    and it has the lock hazard above.
  - A long-lived "zygote" helper that forks per run. Faster, but it's another process to
    health-check, restart and clean up, and M1's hardest bugs were lifecycle bugs. Revisit
    only if the benchmark disappoints.
- **Doc impact on sign-off:** `CLAUDE.md` (Async bullet) and `ARCHITECTURE.md` ("Sync warden
  vs. async server").

> **Amendment 2026-10-25 (step 3, [`M2-step3-warden-core.md`](M2-step3-warden-core.md)
> S3-D4):** the launch is `vestibule-warden -S -P -c "exec(open(<fd>,'rb').read())"`.
> - **Not `-I`:** isolated mode ignores `PYTHONHOME`, and without it the interpreter can't find
>   its own prefix under a neutral `argv[0]`. That stays hidden on Ubuntu (`/lib` → `usr/lib`),
>   and on uv's 3.11 it prints a warning.
> - **The minimal environment** is now exactly one variable, `PYTHONHOME`.
> - **The helper's own output:** its stdout is `/dev/null` and its stderr is the server's log.
>   The guest's pipes are extra fds that init puts on 1 and 2.

### N2 (signed off 2026-10-14) — The rootfs is M1's pinned images, fetched once by an explicit setup command

`vestibule-mcp setup` (exact CLI shape in the step-2 plan) downloads the two images M1
already pins by digest (`DEFAULT_IMAGE_PYTHON` and `DEFAULT_IMAGE_NODE` in `config.py`). It
goes straight to the registry, **without Docker**, and uses the stdlib only. It:
1. picks the `linux/<this arch>` manifest from the pinned index;
2. checks every blob's sha256 against the digest that named it;
3. keeps the image config's `Env` (it's digest-verified too; N9 needs it);
4. unpacks the layers in order into `~/.vestibule/rootfs/<digest>/`, applying OCI whiteouts;
5. renames the tree into place once it's complete.

`python` and `bash` guests run in the python image and `node` guests in the node image,
exactly as in M1. Either backend gives the same Python 3.12 and Node 22, so the `run_code`
description stays true for both. D2's rule holds: **nothing is ever downloaded inside a tool
call.** A missing rootfs gives an actionable message (§5).

- **Why:**
  - Nothing from the host filesystem is visible to the guest, so the acceptance line "no
    host filesystem visibility" is true without a footnote.
  - All three languages work on any Linux host, including hosts without Node (this one has
    none).
  - One pin serves both backends.
- **Cost:** a hand-written unpacker (§6) and a few hundred MB of disk (measured in step 2).
  Plus a one-time network step at setup, as M1 needs `docker pull`.
- **Rejected:**
  - (a) Bind-mounting the host's `/usr` read-only (`ARCHITECTURE.md`'s MVP). The guest would
    see host software, `/etc` would have to be hand-built, many hosts have no `node`, and the
    Python version would differ from the container backend's.
  - (c) `docker export`. It needs Docker on Linux, which `CLAUDE.md` forbids requiring.
  - Private registries. Only anonymous token auth is supported; `HTTPS_PROXY` works because
    `urllib` honors it.

> **Amendment 2026-10-24 (step 2, [`M2-step2-rootfs.md`](M2-step2-rootfs.md) S2-D2, S2-D5,
> S2-D10):**
> - **Item 4:** whiteouts are **refused**, not applied (S2-D2). Neither pinned image has one,
>   and supporting them means deleting directories inside an untrusted tree. A re-pin that
>   brings one fails `setup`, which M1's re-pin procedure now runs.
> - **Layout:** the tree is `~/.vestibule/rootfs/<index-hex>-<arch>/`, with its marker
>   `<index-hex>-<arch>.json` beside it (S2-D5). Setup adds `workspace/`, `sandbox/` and an
>   `etc/hosts` with the localhost lines (S2-D10).
> - **Measured:** about 123 MB to download and 357 MB on disk for both images.

### N3 (signed off 2026-10-14) — Resource limits come from a systemd-delegated cgroup; the spike's numbers pick the mechanism

An unprivileged process can create cgroups only inside a subtree delegated to its user. The
measurements: anything launched through `wsl.exe` lands in `/init.scope`, which is
root-owned and unusable, while `systemd-run --user --scope` got a user-owned scope with cpu,
memory and pids delegated. Two mechanisms; **step 0 times both, and the numbers decide**:

- **(i) Per-run scope (preferred).** The helper starts paused, blocked on its sync pipe. The
  server asks the systemd user manager for a transient scope with `PIDs=[helper]`,
  `Delegate=yes` and `RuntimeMaxSec=<timeout_s + slack>`. It waits, with a time bound, until
  `/proc/<helper>/cgroup` shows the scope, then releases the helper.
  - The server process never moves.
  - systemd's `RuntimeMaxSec` is a deadline enforced outside the server, which no guest can
    stop.
- **(ii) One scope per server.** The first native run moves the server process itself into a
  delegated scope.
  - One D-Bus call per process, then each run is a `mkdir`.
  - **Side effects:** the server and every sandbox silently leave the client's own cgroup
    (for example, a `MemoryMax=` a user put on their agent). The move is also process-wide
    and irreversible; in tests, it moves pytest itself.
- **Rule:** (i) unless it adds more than **50 ms** to the median cold start in the spike. If
  (ii) wins, N3 comes back to Victor with its side effects stated.
- **Either way:** if the server is already inside a writable delegated subtree (for
  example, started by a unit with `Delegate=yes`), it uses that directly with no D-Bus call.
  That's also the only option that stays inside the client's cgroup.
- **Finding the bus:** the reference MCP client starts stdio servers on POSIX with only
  `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`. That was checked in the installed SDK,
  and under that environment `busctl --user` can't find the bus. When
  `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` are unset, the server derives
  `/run/user/<uid>/bus` and uses it only if it's a socket owned by this uid. §9 criterion 17
  launches the server with exactly that environment.

Layout. "Run cgroup" means the per-run scope in (i), or `run-<id>` inside the server's scope in (ii):

```
<run cgroup>/          envelope: guest limits + a small fixed helper allowance;
                       writing 1 to cgroup.kill ends the whole run
  supervisor/          the warden helper
  guest/               exact limits: memory.max = mem_mb, memory.swap.max = 0,
                       pids.max = pids_max, cpu.max = cpu_pct of one CPU
    sandbox/           init + guest; their cgroup-namespace root, so the limit
                       files sit one level above it, out of the guest's reach
```

- **The envelope** bounds the helper's work too, including work a guest provokes (N4's
  notifications), without taking it from the guest's own budget.
- **Every run, the server reads every limit file back.** `native` is claimed only for
  limits the kernel actually holds (S7-D3's rule, carried over).

> **Amendment 2026-10-14 (step 0, spike A2 and the N3 timing):**
> - **Mechanism (i) is chosen by the rule.** The per-run scope via `busctl` measured a
>   75.4 ms median, against 54.8 ms with no cgroup and 51.3 ms for (ii). That's under the
>   50 ms threshold.
> - **A2:** systemd removes an emptied per-run scope together with our sub-cgroups, so no
>   `rmdir` is needed. The helper reads `memory.events` and `memory.peak` before it exits and
>   reports them in the status document.
- **Rejected:**
  - `/sys/fs/cgroup/vestibule/<name>` (`ARCHITECTURE.md`): not writable without root.
  - `setrlimit` in place of cgroups: nothing in rlimits caps a process tree's total memory,
    and `RLIMIT_AS` breaks Node's large virtual reservations.
- **With no systemd user manager**, native runs without limits and says so (N5). If the
  spike confirms `RLIMIT_NPROC` is counted per user namespace (5.14+), it is set to about
  `pids_max` there as a fork-bomb backstop, so a bomb can't exhaust the user's own process
  table.

> **Amendment 2026-10-26 (step 4, [`M2-step4-cgroups.md`](M2-step4-cgroups.md)):**
> - **Mechanism (i) as built (S4-D3–S4-D6):** `busctl --user call … StartTransientUnit` with
>   the properties above, plus `CollectMode=inactive-or-failed` (a scope that ends badly is
>   still removed) and `KillSignal=SIGKILL` (init ignores SIGTERM). The guest's limits are
>   written on `guest/`, the envelope is the scope's own `MemoryMax`, `TasksMax` and
>   `CPUQuotaPerSecUSec`. One 5 s bound covers the whole placement; past it, or on any
>   failure, the run goes on without limits and says so.
> - **No "either way" path (S4-D1, Victor):** every run gets its own scope or runs without
>   limits. Using a delegated subtree the server is already in would mean moving the server
>   into a leaf below it, mechanism (ii)'s side effect. Sandboxes therefore run outside the
>   client's cgroup.
> - **"Every limit file back"** means the guest's: the envelope isn't read back, and no label
>   claims it (S4-D6).
> - **The `RLIMIT_NPROC` backstop** is set only on Linux 5.14 and later, this bullet's own
>   condition (S4-D9). It doesn't make `pids` count as applied.
> - **A2 corrected (S4-D8):** the helper reads `memory.events` only (`oom` and `oom_kill`);
>   `memory.peak` waits for M3's usage report.

### N4 (signed off 2026-10-14) — seccomp: our own libseccomp binding, an allowlist derived from Docker's, required for native

- **Binding:** a small `ctypes` module over the system's `libseccomp.so.2`
  (`seccomp_init`, `seccomp_rule_add`, `seccomp_load`, the `seccomp_notify_*` calls).
  libseccomp still compiles the BPF, so "never hand-assemble BPF" holds.
  - Rejected: `pyseccomp`, which `CLAUDE.md` names today. It's one file at 0.1.2, three
    releases total, and its classifiers end at Python 3.8 (checked 2026-10-14). That's a
    stale trust cost for ~100 lines we can own.
  - Also rejected: vendoring it.
  - The `seccomp` extra leaves `pyproject.toml`.
- **Required for native (Victor, 2026-10-14).** seccomp is a **hard** control. D3 defines
  hard as "changes what hostile code can reach", and this filter blocks nested user
  namespaces, a main route to kernel privilege escalation. No libseccomp means native is
  unavailable, and selection falls through to the container backend. It costs almost
  nothing: libseccomp2 is a systemd dependency on Debian, Ubuntu, Fedora and Arch.
  `CLAUDE.md` and `PLAN.md` change "optional seccomp" to "required for native".
- **Allowlist, derived from Docker's default profile (Victor, 2026-10-14).**
  - Default deny.
  - The allowed set and its argument rules are ported from moby's default seccomp profile,
    pinned to a named moby commit with Apache-2.0 attribution in the module.
  - **Why:** M1's acceptance already showed these exact images run under that profile
    (`Seccomp: 2`). Any syscall a future kernel adds is denied by default. The docs keep
    saying "allowlist".
  - **Native arch only:** a foreign-arch syscall (`int 0x80` on IA32-emulation kernels, x32)
    hits a **kill-process** bad-arch action, so a 32-bit entry can't go around the filter.
  - **Socket families:** `socket`/`socketpair` are allowed only for `AF_UNIX`, `AF_INET`,
    `AF_INET6` and `AF_NETLINK`. **`AF_VSOCK` is not isolated by the network namespace.** On
    this WSL host a sandbox's vsock `connect` reaches the Hyper-V transport (the Windows
    host); measured by the review. Every other family is denied.
  - `clone3` returns `ENOSYS`, so libc falls back to `clone`, whose namespace flags the
    filter can check.
  - Exact errno choices for unknown syscalls are in the step-5 plan.
- **Who loads it:** init loads the one filter right before forking the guest, and the guest
  inherits it, so no filter code runs in the guest. Init sends the notification listener's
  fd to the helper and closes its own copy.
- **Reporting:**
  - Calls on a named dangerous subset go to seccomp **user notification**: mount and the new
    mount API, `unshare`, `setns`, namespace `clone` flags, `ptrace`, `process_vm_*`,
    `bpf`, the keyring, `perf_event_open`, `userfaultfd`, `io_uring_*`, module, kexec,
    reboot, swap, `open_by_handle_at`, and a denied socket family.
  - The helper records each syscall's name and answers `EPERM`. Other non-allowed calls fail
    with `EPERM` unreported.
  - The step-5 plan measures whether reporting *every* denial stays quiet for innocent code
    (M1's acceptance programs) and may widen the subset.
  - **Bounded:** names are deduplicated. After a fixed number of notifications, the helper
    closes the listener; the kernel then fails the rest itself (`ENOSYS`) with no helper work,
    and the result says reporting stopped. The helper's CPU is bounded by the run envelope
    (N3).
  - If the spike shows notification is unworkable, the fallback is plain `EPERM` with no
    names, and the "denied syscalls reported" criterion is amended with Victor.

> **Step 0 (2026-10-14):**
> - **Notification works** through the libseccomp ctypes binding, with init handing the
>   listener fd to the helper. The cap and dedup were verified, and 256 notifications stop
>   cleanly.
> - **The profile now lives in `moby/profiles`.** Spike pin: commit `6fe7deb`.
> - **Two questions go to the step-5 plan:**
>   - Node probes `io_uring_setup` at every start, so `io_uring_*` must be denied
>     *silently*, or every node result reports it.
>   - Moby allows `ptrace` and `process_vm_*` on kernels ≥ 4.8; this subset denies them.
>     Victor decides in step 5.

> **Amendment 2026-10-27 (step 5, [`M2-step5-seccomp.md`](M2-step5-seccomp.md)):** both
> questions resolved, and the mechanism built.
> - **Q1 — `io_uring_*` denied silently** (S5-D2): excluded from the allowlist and from the
>   notified subset, so default-deny returns `EPERM` unnamed; a node hello reports nothing.
> - **Q2 — `ptrace`/`process_vm_*` allowed on ≥ 4.8** (S5-D1, Victor): follow Docker, for
>   parity (native would otherwise be stricter than the `container` floor on the same host) and
>   because a cap-less, `no_new_privs`, non-dumpable-PID-1 guest can only ptrace its own
>   descendants. Criterion 9's denied-and-named example is `mount`, not `ptrace`.
> - **The binding is `src/vestibule/backends/native_seccomp.py`** (our own ctypes over
>   `libseccomp.so.2`); the profile is vendored and sha256-pinned beside it. The helper compiles
>   the filter before it forks init (so no ctypes type materializes after `pivot_root`); init
>   loads it and hands the listener fd to the helper over the existing socketpair.
> - **`seccomp` is verified honestly:** init reports its own `/proc/self/status` `Seccomp:` mode
>   and the server requires `2`; the guest, forked after the load, inherits the filter (S5-D6).
>   `socket`/`socketpair` are restricted to the four families (S5-D4) — `AF_VSOCK` is named.
> - **`socketpair` is restricted too** (N4 pairs it with `socket`), and the `pyproject` `seccomp`
>   extra is removed (N4).
- **Guest-planted symlinks (BUILD_LOG S7-D2): no seccomp change.** Denying
  `symlink`/`symlinkat` everywhere is the only structural fix, and it breaks `python -m venv`
  and npm's `.bin` links inside the workspace. M1's measures stay: `read_workspace` refuses
  links, the tool description warns, and the README says to treat the workspace as untrusted.
  Revisit in M4's `SECURITY.md`.

### N5 (signed off 2026-10-14) — Tiers, labels and selection order on Linux

Same idea as D3: hard controls block, soft controls degrade loudly.

- **Hard tier.** Any failure means native is unusable for the run. The helper **aborts before
  `execve`** and never runs guest code without these:
  - user namespace with the identity map (N6)
  - mount, PID, network, UTS, IPC and cgroup namespaces
  - `pivot_root` into the rootfs, with the rootfs and `/sandbox` mounted
    `ro,nosuid,nodev` (checked with `statvfs` flags before `execve`)
  - capability sets `CapEff`, `CapPrm`, `CapBnd` and `CapAmb` all 0; `NoNewPrivs: 1`
  - PID 1 non-dumpable (N7)
  - the seccomp filter loaded
  - only `lo` in the network namespace
  - the workspace round-trip
- **Soft tier:** memory, pids and cpu (cgroups), and the `/tmp` and `/dev/shm` size caps.
- **Labels:**
  - `native`: every control is verified, for this run.
  - `native-degraded (limits not applied: …)`: the hard tier holds, some soft control
    doesn't. Same shape as `container-degraded`.
  - **`namespaces-only` is retired.** `CLAUDE.md` golden rule 5 and the `base.py` comment are
    updated to the real set: `none`, `container`, `container-degraded`, `native`,
    `native-degraded`.
- **Checked per run, not only at selection:**
  - The helper's status document carries the stage reached, plus "filter loaded" and the
    mount-flag check.
  - **A failure before `execve`** reports `isolation: none` ("nothing was executed") and
    calls `note_result`, so the next call re-probes. `note_result` is extended to any
    native-tier run that reports `none`. Example triggers: the rootfs deleted mid-session,
    libseccomp removed.
  - **A run with no status document underclaims and is never `native`.** The exact label is
    in the step-3 plan.
- **Order for `VESTIBULE_BACKEND=auto` on Linux: strongest verified guarantee first.**
  1. native, full
  2. container, full
  3. native-degraded
  4. container-degraded
  5. `Blocked:` with both reasons

  A full container beats degraded native because limits protect the host itself: without a
  memory cap, a native sandbox can trigger the *host's* OOM killer. Native is probed first;
  the container probe runs only when native isn't full.
- **Off Linux:** `auto` behaves exactly as in M1. Native is never probed or reported.
- **Ubuntu's restricted user namespaces (Victor, 2026-10-14).** On stock Ubuntu 23.10+,
  `kernel.apparmor_restrict_unprivileged_userns=1`: `unshare` succeeds but the namespace has
  no capabilities, so `mount` fails. The probe actually tries the sandbox, so it fails safe:
  it falls back to the container backend with a message naming the cause.
  - **M2:** tests this by injection (this WSL kernel has no AppArmor).
  - **M4:** the fresh-machine test runs on a stock Ubuntu 24.04+ VM.
  - **README:** says which distros get native out of the box.
  - **No AppArmor profile is documented:** it would grant user namespaces to every `python3`
    on the system.

> **Amendment 2026-10-25 (step 3, [`M2-step3-warden-core.md`](M2-step3-warden-core.md)
> S3-D8):**
> - **The open label:** a run with no status report is `none`, "no isolation is claimed for
>   this run". Nothing confirms what applied.
> - **Per-run verification:** init aborts before the guest exists on its own checks (mount
>   flags, capabilities, `no_new_privs`, not dumpable, only `lo`, no controlling terminal). The
>   server then checks the reported namespace inodes, identity maps and mount table against its
>   own values.

### Fixed decisions (one defensible answer each)

- **N6 — Identity uid map.**
  - Inside the namespace the guest has the host uid and gid (`<uid> <uid> 1`, gid likewise),
    not root. `setgroups` is denied first.
  - Capabilities exist only inside the new namespace and only until the guest's `execve`; a
    non-root `execve` clears them by kernel rule, and the bounding set is dropped explicitly
    too.
  - Workspace files come out owned by the user, matching M1's `--user <uid>:<gid>` on
    Linux. (`ARCHITECTURE.md`'s `0 <uid> 1` makes the guest root inside.)
  - **Parity gap, documented:** the host user's *supplementary* groups still count for
    permission checks. Docker's `--user uid:gid` has none. No unprivileged process can drop
    them (`setgroups` must be denied before the gid map is written). Nothing those groups
    unlock is visible inside the jail: the only host-backed paths are the rootfs, the
    workspace and bind-mounted `/dev` nodes. Criterion 10 records it.
- **N7 — Process shape, and PID 1 hardened against its own guest.** server → **helper**
  (supervisor; never `execve`s) → **init** (PID 1, a minimal reaper standing in for M1's
  `--init`) → **guest** (`execve`s the interpreter). The guest is the same uid as init, so
  without hardening it could open `/proc/1/*`. The review demonstrated forging the status
  report, reading the server's environment, and resolving a host path through
  `/proc/1/cwd`.
  - **Helper:** started with a minimal environment (N1). It alone holds the status pipe and
    the notification socket. After forking it closes its copies of the guest's stdout and
    stderr, so its own diagnostics never reach the model and pipe EOF means the sandbox's
    writers are gone. Its diagnostics travel in the status document instead.
  - **Init, in order:**
    1. Block on a sync pipe from the helper; EOF means exit. The same pipe says "you're in
       the cgroup". (`getppid()` returns 0 for PID 1 of a new namespace, so it can't detect
       a dead parent.)
    2. `close_range` every fd it doesn't need.
    3. `chdir("/")` after `pivot_root`.
    4. Drop all capabilities.
    5. `PR_SET_DUMPABLE 0`, which makes `/proc/1/*` unreadable to the guest.
    6. `no_new_privs`.
    7. Load the seccomp filter.
    8. Fork the guest.
  - **Guest:** `close_range(3, ~0)` before `execve`, so its fd table is exactly `{0,1,2}`.
  - **`/proc/1/cmdline`** is readable in every mode, and as drafted it shows the helper's
    interpreter and venv path, which leaks the host username. The step-3 plan picks how to
    show no host path (for example, init re-executing a tiny reaper from the rootfs).
    Criterion 10 checks it.

  > **Amendment 2026-10-25 (step 3, S3-D4 and S3-D7):**
  > - **`/proc/1/cmdline`:** the neutral launch shows no host path. The username is still
  >   visible in the guest's `/proc/self/mountinfo` (bind-mount sources), as in Docker; that is
  >   noted for M4's `SECURITY.md`.
  > - **Init also:**
  >   - resets SIGINT to `SIG_DFL`, so a guest's `kill -INT 1` is ignored;
  >   - calls `setsid()`, so the guest can't open the server's terminal through `/dev/tty`;
  >   - puts the guest's pipes on fds 1 and 2.
  > - **The guest** resets SIGPIPE and SIGXFSZ to `SIG_DFL`, and does its own PATH search.
  > - **The "go" handshake** uses the helper/init socketpair, not a separate pipe.
- **N8 — Kill paths no stopped or hostile guest can block.**
  - **Timeout or output overflow:** the server writes `1` to the run cgroup's `cgroup.kill`
    (Linux 5.14+). `SIGSTOP` can't block it, which closes M1's S7-D5 watchdog residual for
    native. In mechanism (i), systemd's `RuntimeMaxSec` backs this up from outside.
  - **Without cgroups:** the server kills the helper; init has `PR_SET_PDEATHSIG(SIGKILL)`;
    and when PID 1 dies, the kernel kills every process in its namespace.
  - **Server hard-killed:** the helper has `PR_SET_PDEATHSIG(SIGKILL)` toward the server,
    with a `getppid()` check right after to close the start-up race. A helper
    self-deadline is the last backstop.
  - PDEATHSIG fires when the *thread* that forked the child exits, so the helper is always
    spawned from the event-loop thread.
  - **No orphan reaper:** nothing outlives its server. Empty run cgroups are removed.

  > **Amendment 2026-10-25 (step 3, S3-D7) — without cgroups, the order is reversed:**
  > - the server sends SIGTERM to the helper, which SIGKILLs init through its pidfd;
  > - the kernel reports init's exit only once the whole PID namespace is empty, so the
  >   helper's exit means the sandbox is gone, and the concurrency slot is released then;
  > - SIGKILL to the helper, 5 s later, stays as the fallback.

  > **Amendment 2026-10-26 (step 4, S4-D2, Victor) — no `cgroup.kill`:** step 3's stop above
  > is the only stop, with or without a cgroup. Killing PID 1 empties its namespace, stopped
  > processes included, and the helper's exit is the one signal that it has. systemd's
  > `RuntimeMaxSec` (with `KillSignal=SIGKILL`) and the helper's self-deadline are the two
  > outside deadlines. Nothing in M2 needs Linux 5.14 for stopping any more.
- **N9 — Parity with the container profile.**
  - **Paths:** `/workspace` (rw, or ro under `VESTIBULE_WORKSPACE_RO`); the script at
    `/sandbox/main.<ext>` (read-only); workdir `/workspace`; `/tmp` a size-capped tmpfs.
  - **Environment:** the image config's `Env` (its `PATH` and `LANG`) plus the profile's four
    (`HOME=/tmp`, `TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, `NODE_OPTIONS=`) plus
    `HOSTNAME`. That's M1's set; nothing is inherited from the host. The review found the
    four alone break `subprocess.run(["python", …])` and Node's `spawn('node')`.
  - **`/dev`:** `null`, `zero`, `full`, `random`, `urandom` and `tty` bind-mounted; `/dev/shm`
    a 64 MiB tmpfs (Docker's default; `multiprocessing` needs it); `/dev/pts` a new devpts
    instance with `/dev/ptmx` (`pty` needs it); the `fd`, `stdin`, `stdout` and `stderr`
    links.
  - **Other:** a fresh `/proc`; no `/sys`; hostname `vestibule`.
  - **Output and timeouts:** the same output caps and overflow kill as M1. The startup grace
    on the timeout shrinks (native setup takes milliseconds); the step-3 plan fixes it. The
    outer deadline stays `timeout_s + 30`.

  > **Amendment 2026-10-25 (step 3, S3-D1–S3-D3, S3-D7, S3-D10):**
  > - **`/sandbox`** is a per-run tmpfs that init writes the script into, then makes
  >   read-only. No host temp dir.
  > - **`/proc`** gets Docker's masks and read-only paths.
  > - **`lo` is up**, as in Docker's `--network none`.
  > - **No controlling terminal.**
  > - **The startup grace is 2 s.**
- **N10 — No new tool surface.** Still two tools. The config surface grows only as in §7.
- **N11 — stdout discipline (golden rule 1).** The guest's stdout and stderr are pipes the
  server reads, never the server's own stdout. The helper reports on a separate status pipe
  as one JSON document, read with a size cap and a time bound.

---

## 3. Native execution profile (exact shape)

Shown for mechanism (i). In (ii) the helper is born inside the server's scope and moves
itself into `run-<id>/supervisor` instead of waiting for systemd.

```
server (asyncio)
 │ spawn: <python> -I <warden helper>  env=minimal, stdin=DEVNULL,
 │        stdout/stderr = output pipes, fd 3 = status pipe, fd 4 = sync pipe
 │ ask systemd: scope PIDs=[helper] Delegate=yes RuntimeMaxSec=…; wait (bounded)
 │ mkdir supervisor/ guest/ guest/sandbox/; write limits; read them back; release the helper
 ▼
helper (fresh interpreter, single-threaded)
 1. PR_SET_PDEATHSIG(SIGKILL); exit if getppid() is no longer the server
 2. move self into supervisor/
 3. unshare(NEWUSER|NEWNS|NEWNET|NEWUTS|NEWIPC|NEWPID)
 4. setgroups=deny; uid_map "<uid> <uid> 1"; gid_map "<gid> <gid> 1"
 5. fork ───────────────────────────▶ init (PID 1)
    move init into guest/sandbox/      6. block on the sync pipe; PR_SET_PDEATHSIG(SIGKILL)
    then signal it on the sync pipe     7. close_range the fds it doesn't need
    close own copies of fds 1, 2        8. unshare(NEWCGROUP)  → root = guest/sandbox
                                        9. make / rprivate; build the new root: rootfs (ro,nosuid,nodev),
                                           /proc, /dev (N9), /tmp + /dev/shm tmpfs, /workspace, /sandbox (ro)
                                       10. pivot_root; detach the old root; chdir("/")
                                       11. sethostname; drop all caps; PR_SET_DUMPABLE 0; no_new_privs
                                       12. verify mount flags; load seccomp; send the listener fd; close it
                                       13. fork ─────▶ guest: close_range(3, ~0);
                                           reap loop;          execve(interpreter, /sandbox/main.<ext>)
                                           exit with the guest's status
 14. supervise: answer notifications (EPERM + record, bounded); wait for init
 15. status document on fd 3: stage reached, checks, exit status / signal, OOM kill
     (guest memory.events), denied syscalls, helper diagnostics
server: collect stdout/stderr (2× cap; overflow ⇒ cgroup.kill); timeout ⇒ cgroup.kill;
        read the status (bounded); remove the run cgroup; build the RunResult
```

Exact syscall order, flags and fd numbers come from the step-3/4/5 plans after the spike.

> **Amendment 2026-10-14 (step 0, [`M2-step0-spike.md`](M2-step0-spike.md) A1, A3–A5):**
> - **A1:** the helper unshares only `NEWUSER|NEWPID`, and **init** unshares
>   `NEWNS|NEWNET|NEWUTS|NEWIPC|NEWCGROUP` (step 8 above). `pivot_root` moves the root of
>   every process in its mount namespace, so a helper that shared init's namespace was
>   pulled into the sandbox root and lost the host cgroup files.
> - **A3:** `/workspace` is bind-mounted `nosuid,nodev`.
> - **A4:** the helper is self-contained stdlib code fed through a pipe and run under the
>   neutral `argv[0]` `vestibule-warden`, so `/proc/1/cmdline` shows no host path.
> - **A5:** every import happens before `pivot_root`.

> **Amendment 2026-10-25 (step 3, [`M2-step3-warden-core.md`](M2-step3-warden-core.md)):**
> - **A4 corrected (S3-D4):** `-S -P` with `PYTHONHOME`, not `-I`.
> - **A5 widened (S3-D5):** no call after `pivot_root` may import lazily, not just no import
>   statement. That rules out `ctypes.Structure` subclasses made late (3.14), `os.execvpe`
>   (it imports `warnings`) and `os.closerange` (it loops on uv's 3.11; `close_range` is
>   used).
> - **The chain above as built:**
>   - binds are non-recursive;
>   - the tree is read-only from its first remount;
>   - bind remounts keep the source mount's locked `ro`/`noexec` (S3-D6);
>   - init checks itself before the guest exists, and sends `ready`, `error` and `exit` lines
>     for the helper to relay (S3-D8).

> **Amendment 2026-10-26 (step 4, [`M2-step4-cgroups.md`](M2-step4-cgroups.md) S4-D3,
> S4-D5, S4-D7, S4-D8):**
> - **The helper waits for its release** before it unshares anything: one JSON object on the
>   sync pipe, with the `guest/sandbox` path (or none) and the `RLIMIT_NPROC` value (or none).
>   The server moves it into `supervisor/` (step 2 above is the server's write).
> - **The helper moves init into `guest/sandbox/`** before "go". If that fails, the run is
>   `none` and nothing runs.
> - **Init reports `/proc/self/cgroup`** in `ready`, read before `unshare(NEWCGROUP)`. A limit
>   counts only if that's the path the server made and the limit read back there.
> - **The OOM report** is the helper's own line after init's exit: `guest/`'s `oom` and
>   `oom_kill`.
> - **No `cgroup.kill`, and the server removes no cgroup** (N8's and §4's amendments).

## 4. Run lifecycle

1. **Concurrency:** the selector's single shared semaphore (S7-D1) covers native runs, with
   the same 5 s bounded wait and `Blocked:` on refusal (S4-D1).
2. **Per run:** a random ID (`secrets.token_hex(8)`), a host temp dir for the script (as D9),
   and the run cgroup.

   > **Amendment 2026-10-25 (step 3, S3-D1, Victor):** no host temp dir. The script travels in
   > the run config (a memfd), and init writes it into a read-only tmpfs at `/sandbox`. A
   > native run creates no host files, so cleanup and the shutdown hand-off have none to
   > remove.
3. **Run:** spawn the helper; stream and cap the output; enforce the timeout (N8); collect the
   status and apply N5's per-run verdict.
4. **Cleanup:** the temp dir and the cgroup are removed by a detached task that never delays
   the result. The concurrency slot is released only when cleanup is done (M1's P2 rule).
5. **Shutdown (stdin EOF *or* `SIGTERM`):** write `cgroup.kill` for every live run, then the
   same short, non-cancelling wait as S7-D1.

> **Amendment 2026-10-15 (step 1b, [`M2-step1-linux-baseline.md`](M2-step1-linux-baseline.md)
> S1-D5):** POSIX shutdown is now a synchronous hand-off: stdin EOF, `SIGINT`, `SIGTERM`
> or `SIGHUP` → `SELECTOR.cleanup_job()` → kill the tracked CLIs → start the detached
> `vestibule.cleanup` → `os._exit(0)` within milliseconds. Real clients allow 100 ms
> (Claude Code) to 2 s (the Python SDK).
> - **Native runs need no shutdown work** beyond their temp dirs, which go to the cleaner.
>   When the server dies, the PDEATHSIG chain ends the helper, init and the guest, and
>   systemd removes the emptied per-run scope (step-0 spike: everything gone within 12 ms).
>   No `cgroup.kill` at shutdown.
> - **What step 6 must add:** a native backend's `cleanup_snapshot()` (temp dirs only) for
>   `cleanup_job()`.

> **Amendment 2026-10-26 (step 4, S4-D2, S4-D6):** cleanup removes no cgroup. systemd removes
> the emptied scope with our sub-cgroups when the helper exits, and `CollectMode=inactive-or-
> failed` removes one that failed. The finisher is step 3's. Shutdown writes no `cgroup.kill`.

## 5. Backend selection & probing on Linux

**The probe is a real run through the normal native path**, so it can't drift from what real
runs get. It's a `bash` guest in the python rootfs that:
- does the workspace round-trip (RO mode: reads work, writes fail);
- prints its uid/gid/groups, the `Cap*`, `NoNewPrivs` and `Seccomp` lines, its open fds, its
  network interfaces and `/proc/self/mountinfo`;
- tries `/proc/1/environ`;
- writes a byte to `/tmp`.

The server checks it all. The mount list must match the expected set and flags exactly,
alongside the cgroup readback from outside. Verdict per N5. Selection is lazy, cached and
lock-guarded as in M1, and failures re-check after S5-D1's 30 s cooldown.

Actionable messages (each names the fix):
- **No rootfs:** "run `vestibule-mcp setup` once (downloads ~N MB)". Falls through to the
  container backend if one works.
- **User namespaces refused:** names the cause it can see:
  - `apparmor_restrict_unprivileged_userns = 1`: Ubuntu's policy, which needs root to lift,
    so the container backend is used.
  - `max_user_namespaces = 0`: disabled.
- **No libseccomp:** native unavailable (hard tier). Container backend, or `Blocked:`.
- **No systemd user manager or no delegation:** "resource limits unavailable" in the
  degraded detail.

`VESTIBULE_BACKEND=native` forces native: degraded is allowed and reported, with no
fallback. `VESTIBULE_BACKEND=container` forces M1's selection. On a non-Linux host,
`native` is a restart-required refusal ("native isolation needs Linux").

## 6. Rootfs setup (N2)

- **Unpacker: hand-written, because the stdlib's safe filter can't be used.**
  `tarfile.data_filter` rejects absolute symlinks (checked on 3.14.4), and Debian-family
  trees are full of them (`/usr/bin/awk -> /etc/alternatives/awk`). Rules:
  - every entry is resolved component by component from a root directory fd with
    `O_NOFOLLOW`, so a symlink from an earlier entry is never followed;
  - symlinks are stored verbatim;
  - hardlinks only to already-unpacked regular files inside the tree;
  - no device nodes; setuid, setgid and sticky bits stripped;
  - whiteouts applied.
  - **Fixtures** cover single bad entries (`../`, absolute names, devices, setuid) **and
    multi-entry attacks**: a symlink `a -> /home/u` followed by `a/.bashrc`, and a hardlink
    whose path crosses a symlink.
  - zstd layers need Python 3.14's `compression.zstd` while `requires-python` is ≥ 3.11, so
    they're refused with a legible message. The pinned images use gzip; step 2 checks.
- **Registry client:** the bearer token goes in an *unredirected* header, so blob redirects
  to the CDN never carry it. The review checked that `urllib` otherwise copies it, and that
  presigned URLs can reject it.
- **Cache integrity:** a completed tree carries a marker naming its manifest digest; partial
  trees are ignored and replaced. The marker lives *beside* the tree, never inside it, or the
  guest would see it in `/` (spike A6). Files are made read-only after unpacking, as hygiene;
  N5's mount check is the real guard.
- **Trust statement:** the cache belongs to the user, so anything running as the user can
  change it, just as with the user's own Docker images. The digest check proves what was
  downloaded, not what is on disk later.
- **Tests:** offline unit tests with fixture layers, plus one `network`-marked test that
  fetches the real pinned python image.

> **Amendment 2026-10-24 (step 2, [`M2-step2-rootfs.md`](M2-step2-rootfs.md) S2-D2, S2-D5,
> S2-D6, §4.3):**
> - **Refused, failing the image:** whiteouts, a directory replaced by a non-directory and a
>   missing parent, alongside device nodes and FIFOs. A hardlink's linkname follows the same
>   rules as a name.
> - **Cache:** the marker is `<index-hex>-<arch>.json` beside the tree, written last, after
>   an `os.sync()`. Setup only ever touches its own names in the directory.
> - **Tests:** the `network` test fetches **both** pinned images, and a `docker` + `network`
>   test compares each tree, entry by entry, with `docker export` of the same digest.
> - Both pins use gzip layers.

## 7. Server, config and repo changes

- **`server.py`:** a POSIX `SIGTERM` handler that runs the same shutdown as stdin EOF (the
  BUILD_LOG known issue). `_format_result` needs no change.
- **`select.py`:**
  - generalized from "container backends" to any backend (tracking, the shared semaphore,
    `cleanup_job()`; `aclose` stays Windows-only and container-only, per step-1b S1-D5);
  - the native probe and N5's order;
  - `note_result` extended to native-tier runs that report `none`.
- **`config.py`:**
  - `VESTIBULE_BACKEND` accepts `auto | native | container | naive`.
  - New `VESTIBULE_ROOTFS_DIR` (default `~/.vestibule/rootfs`).
  - **The workspace and the rootfs directory must not overlap** (neither inside the other):
    a `ConfigError`. Otherwise a guest that owns every file could rewrite the cache and
    defeat the pin.

    > **Amendment 2026-10-24 (step 2, [`M2-step2-rootfs.md`](M2-step2-rootfs.md) S2-D9):**
    > not a `ConfigError`: that would block every tool for working M1 setups (for example
    > `VESTIBULE_WORKSPACE=~`). `setup` refuses an overlap, and step 6's native selection
    > will too, falling back to the container backend.
  - No knob turns seccomp or any hard control off.
- **`pyproject.toml`:** the `seccomp` extra goes. New pytest markers: `native` (Linux, rootfs
  present, user namespaces working; skips with the reason otherwise) and `network`.
- **`.gitattributes`:** `*.sh text eol=lf`. `core.autocrlf` is `true` here and there's no
  eol rule today, so any `.sh` M2 adds would get CRLF and break under WSL bash (M1's step-5
  bug class).
- **`.claude/agents/gate.md`:** ruff, mypy and pytest on **both** platforms: the Windows
  `.venv`, and WSL `~/.venvs/vestibule` with its own mypy cache dir.
- **`base.py`, `CLAUDE.md`:** the isolation-value list (N5); the seccomp and async wording
  (N1, N4).

## 8. Corrections to `ARCHITECTURE.md` (applied in step 7)

1. **Stage 8 can't happen where it's drawn.** After `pivot_root` the cgroup filesystem is
   gone from the guest's view. And cgroup v2 lets a process be moved only by someone who
   can write the common ancestor's `cgroup.procs`. Placement happens from outside, before
   the namespaces close.
2. **`/sys/fs/cgroup/vestibule/<name>` needs root:** it's a delegated systemd scope instead
   (N3).
3. **`0 <uid> 1` makes the guest root in its namespace:** it's the identity map instead (N6).
4. **Rootfs option (a) is not the MVP:** N2.
5. **"Run it in an executor":** a helper process instead (N1).
6. **"seccomp via pyseccomp, optional":** our own libseccomp binding, still an allowlist
   (Docker-derived), and required for native (N4).
7. **PID 1 needs explicit hardening** (N7); the stage list doesn't mention it.

## 9. Acceptance criteria (supersede PLAN.md §M2's list)

On WSL Ubuntu 26.04 (kernel 6.6), through `server.call_tool` like M1's suite:

1. **Hello world:** python, bash and node ⇒ `isolation: native`.
2. **No host filesystem visibility:**
   - A canary at a unique absolute host path (e.g. `$HOME/.vestibule-canary-<random>/
     id_ed25519`, removed after the test) is unreachable: by absolute path, through
     `/proc/1/root` and `/proc/1/cwd`, and with `find /`.
   - `/proc/self/mountinfo` equals the expected mount list.
   - `/proc` shows only sandbox PIDs; `/sys` is absent.
   - (The review showed a `~/.ssh` probe can't fail: `HOME=/tmp`.)
3. **Network:** TCP to an IP, DNS resolution, a UDP send and **`socket(AF_VSOCK)`** all fail;
   the only interface is `lo`.
4. **Memory bomb:** OOM-killed inside its own cgroup (`oom_kill` ≥ 1 in `guest/memory.events`);
   the server and the next run are fine.
5. **Fork bomb:** stops at `pids.max`; the run returns; the next run works.
6. **CPU:** `cpu.max` reads back as requested; a busy loop stays near `cpu_pct`.
7. **Disk:** `/tmp` fills to its cap, then `ENOSPC`. Writes to the rootfs and to `/sandbox`
   fail with `EROFS`.
8. **Timeout:** an infinite loop *and* a self-`SIGSTOP`ped guest both return `[timed out …]`
   within `timeout_s + 30`, and nothing survives: the run cgroup is gone, with no helper,
   init or guest PID left.
9. **Denied syscalls:**
   - `unshare`, `mount` and `ptrace` attempts get `EPERM` and are listed in the result.
   - A notification storm (many threads looping on `mount`) returns, with deduplicated
     names, the helper's CPU inside the envelope, and "reporting stopped".
   - A foreign-arch syscall kills the process (the step-5 plan picks the exact test).
10. **From inside the guest:**
    - uid and gid are the host user's; supplementary groups as N6 documents.
    - `CapEff`, `CapPrm`, `CapBnd` and `CapAmb` are all 0; `NoNewPrivs: 1`; `Seccomp: 2`.
    - Environment = M1's set (N9).
    - The fd table is exactly `{0,1,2}`.
    - `/proc/1/{environ,fd,cwd,root,mem}` are denied, and `/proc/1/cmdline` shows no host
      path.
    - Nothing in the guest can write to the status pipe.
11. **Server hard-killed mid-run** (`SIGKILL`) ⇒ no guest process survives within 2 s.
12. **Concurrency:** 4 simultaneous runs succeed in 4 distinct PID namespaces.
13. **Degradation, by injection.** Each of these gives N5's order and an honest label or
    `Blocked:`, never `native` for a run that lacked a control:
    - no rootfs
    - the rootfs removed *after* selection (that run reports `none`, and the next call
      re-probes)
    - user namespaces refused (AppArmor-style)
    - no delegation
    - no libseccomp
14. **Cold-start benchmark:** median and p95 over ≥ 50 runs per backend, same host, selection
    already cached, run from a **wheel installed on ext4**. The host and the "Docker Desktop
    on WSL" caveat are stated next to the number.
15. **Both platforms green:** the full suite passes on Linux (container tests included, via
    Docker Desktop's WSL integration) and still passes on Windows, where native tests skip
    with their reason. ruff and mypy clean on both.
16. **Live-agent check:** Linux Claude Code in WSL calls the registered server. It runs
    hellos in all three languages, the network attempt and a memory bomb, tries to read the
    absolute-path canary (absent), and calls `read_workspace`. All report
    `isolation: native`. It also measures how Linux Claude Code shuts the server down (stdin
    close vs `SIGTERM`), which closes the BUILD_LOG known issue.
17. **Minimal-environment launch:** the server started with the MCP SDK's default POSIX
    environment (`HOME LOGNAME PATH SHELL TERM USER` only) still selects `native`, finding
    the user bus itself.

## 10. Implementation order (each step ≈ one commit)

0. **Spike (no repo code).** Throwaway scripts in the scratchpad; the rootfs comes from
   `docker export`, for the spike only. It must:
   - prove §3's chain unprivileged on this host, PID-1 hardening included (the review's
     `/proc/1` experiments must all fail afterwards);
   - time N3's (i) and (ii);
   - confirm bus discovery under the minimal environment;
   - prove the libseccomp ctypes binding with an allowlist, user notification, and init
     handing the listener fd to the helper;
   - check that devpts and `/dev/shm` mount inside the user namespace;
   - check whether `RLIMIT_NPROC` is counted per user namespace;
   - measure the helper's memory and rough cold-start numbers.

   Output: `docs/plans/M2/M2-step0-spike.md`. Anything that contradicts N1–N5 goes back to
   Victor.
1. **Linux baseline:**
   - the WSL venv and the two-platform gate;
   - `.gitattributes` eol rule;
   - the existing suite on Linux (Docker tests included), fixing Linux-only failures — the
     first real run of `os.getuid` and `O_NOFOLLOW`;
   - the POSIX `SIGTERM` handler.
2. **Rootfs:** the registry client, the unpacker and the setup command (N2, §6). Also the
   config overlap check.
3. **Warden core:**
   - the helper, namespaces, filesystem jail and N7's process shape with PID-1 hardening;
   - N8's kill chain without cgroups;
   - the `NativeBackend` server side.

   **Not reachable from the server yet.** Tests build the backend directly. seccomp is hard
   and lands in step 5, so steps 3–4 test through a private constructor parameter. No config
   value or selector path can pass it, and step 6 adds a test proving every selector-built
   native backend loads the filter.
4. **cgroups v2 (N3):** the chosen mechanism, bus discovery, the envelope layout, limits plus
   readback, `cgroup.kill`, the OOM-kill report and the `RLIMIT_NPROC` backstop. *(Built
   without `cgroup.kill`: S4-D2.)*
5. **seccomp (N4):** the binding, the Docker-derived allowlist, socket families, bad-arch
   kill, and notification reporting with bounds.
6. **Selection on Linux (N5, §5) — native becomes reachable here, with everything it needs:**
   - the `native` and `container` backend values;
   - the selector generalized beyond containers (tracking, shared semaphore, and
     `cleanup_job()` per step-1b S1-D5), so shutdown reaches native runs from the first
     commit that can start one;
   - the native probe, tier order, per-run verification with `note_result`, and the
     actionable messages.
7. **Acceptance, benchmark and docs:**
   - §9's tests; the benchmark script and README numbers, with the per-distro availability
     table;
   - §8's `ARCHITECTURE.md` corrections; `CLAUDE.md`/`PLAN.md` wording; a GETTING_STARTED
     Linux section;
   - then the live-agent check with Linux Claude Code (E2).
   
   May split into 7a/7b as in M1.

## 11. Facts measured on the dev host (2026-10-14)

| Fact | Value | Matters for |
|---|---|---|
| Distro / kernel | Ubuntu 26.04.1 LTS on WSL2, kernel 6.6.87.2, 12 CPUs, 15 GB | `cgroup.kill` (≥ 5.14), notification (≥ 5.0) |
| Unprivileged user namespaces | `unshare --user --map-root-user --mount --pid --net --fork` works; `max_user_namespaces` 62141; AppArmor off; Yama `ptrace_scope` 1 | hard tier; the AppArmor case is untestable here (N5) |
| cgroups | v2 only, mounted `nsdelegate`; root controllers `cpuset cpu io memory hugetlb pids rdma` | N3 |
| Where processes land | `wsl.exe`-launched processes and login shells: `/init.scope` (root-owned). `systemd-run --user --scope`: a user-owned scope under `user@1000.service`, with `cpu memory pids` delegated | N3 |
| User bus under the MCP SDK's default env | `busctl --user` fails (no `XDG_RUNTIME_DIR`); works with `XDG_RUNTIME_DIR=/run/user/<uid>` (review) | N3 bus discovery, criterion 17 |
| seccomp | supported; `libseccomp.so.2` installed; no compiler (`gcc` missing) | N4: ctypes only |
| AF_VSOCK in an empty network namespace | `socket` succeeds; `connect(CID 2)` behaves as from the host; `/dev/vsock` exists (review) | N4 socket families, criterion 3 |
| PID 1 with dropped caps, still dumpable | the same-uid guest read `/proc/1/environ`, wrote the status pipe via `/proc/1/fd`, resolved `/proc/1/cwd` to a host path (review) | N7 |
| Helper start cost | one-file stdlib helper 17 ms (ext4) / 22 ms (`/mnt/c`); bare interpreter 9 ms (review) | N1, criterion 14 |
| `tarfile.data_filter` (3.14.4) | rejects absolute symlinks (`AbsoluteLinkError`) | §6 |
| Python | 3.14.4 system; venv works after `python3.14-venv` (Victor installed it 2026-10-14) | E1 |
| Docker in WSL | CLI and server 28.5.1 (Docker Desktop integration); user in the `docker` group | step 1; benchmark |
| Node / Claude Code in WSL | no Linux `node`; `claude` and `npm` on the PATH are the Windows binaries | N2; E2 |
| `/mnt/c` speed | stat of 2,000 files: 0.125 s vs 0.013 s on ext4 | E1 |
| Line endings | `core.autocrlf=true`; `.gitattributes` has no eol rule | §7 |

## 12. Review record

`skeptic` reviewed draft v1 on 2026-10-14. Verdict: "sound with amendments". The review's
experiment scripts are in the session scratchpad. Claims about existing code were checked by
the main session (`select.py:211-219`, `server.py:245-246`, the SDK's
`DEFAULT_INHERITED_ENV_VARS`, `.gitattributes`). Kernel claims were reproduced by the review
on this host and are re-tested in step 0. Every finding was accepted:

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | The guest reaches PID 1 via `/proc/1`: forges the status report, reads the server's environment, resolves host paths | N1 minimal env; N7 PID-1 hardening + fd closing; criterion 10 |
| 2 | med | `native` verified only at selection; no per-run abort or underclaim rules; no mount-flag check | N5 per-run verification, `note_result` extension; §5 mountinfo; criterion 13 |
| 3 | med | seccomp in the soft tier contradicts D3 | **Victor: seccomp is hard for native** (N4, N5) |
| 4 | med | AF_VSOCK isn't isolated by the network namespace | N4 socket-family rule; criterion 3 |
| 5 | med | The user bus is unreachable under the MCP SDK's default environment | N3 bus discovery; criterion 17 |
| 6 | med | Notification work unbounded and outside the guest's limits | N3 envelope; N4 dedup, cap, listener close; criterion 9 |
| 7 | med | The `~/.ssh` canary can't fail (`HOME=/tmp`) | criterion 2 absolute-path canary; criterion 16 |
| 8 | med | "Four env vars" drops the image's `PATH`/`LANG` | N2 keeps the image config `Env`; N9; criterion 10 |
| 9 | med | The acceptance host can't show Ubuntu's AppArmor restriction | **Victor: injection in M2, stock-Ubuntu VM in M4**; README table; no profile (N5) |
| 10 | med | Moving the server escapes the client's cgroup and is irreversible; the per-run scope was rejected unmeasured | N3 rewritten: both timed in step 0, (i) preferred, rule stated |
| 11 | med | The case against an allowlist is contradicted by M1's own evidence | **Victor: allowlist derived from Docker's** (N4); bad-arch kill |
| 12 | low | `getppid()` is 0 for PID 1 | N7 sync pipe |
| 13 | low | Supplementary groups kept | N6 documented gap; criterion 10 |
| 14 | low | The stdlib tar filter is unusable; multi-entry attacks; zstd | §6 unpacker rules and fixtures |
| 15 | low | `urllib` forwards the token on redirect | §6 unredirected header |
| 16 | low | The workspace and rootfs cache can overlap | §7 `ConfigError` |
| 17 | low | Limits on the guest's cgroup-namespace root | N3 layout: limits one level above |
| 18 | low | No-cgroup fork bomb hits the user's process table | N3 `RLIMIT_NPROC` backstop (spike-checked) |
| 19 | low | No `/dev/shm` or `/dev/pts` | N9 parity |
| 20 | low | The benchmark would include `/mnt/c` overhead | criterion 14: wheel on ext4 |
| 21 | low | Steps 3 and 6 out of order | §10: native becomes reachable from the server only in step 6, together with the selector generalization; steps 3–5 test the backend directly |
| 22 | low | CRLF line endings for `.sh` files | §7 `.gitattributes` |
| 23 | low | Isolation-value list not on the doc update list | N5, §7 |
| 24 | low | The helper shares the guest's stderr; unbounded status read | N7, N11 |
