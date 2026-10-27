# M2 step 5 — seccomp: our libseccomp binding, a Docker-derived allowlist, bounded denial reporting

Status: **draft (v2), revised after the `skeptic` review (§7); pending Victor's build sign-off.**
S5-D1 (the ptrace policy, spike Q2) was **resolved by Victor on 2026-10-26: option (B), follow
Docker — `PTRACE_POLICY = "allow"`.** Everything else is fixed: one defensible answer each, most
forced by the spike.

- **v2 fixed what the `skeptic` round found** (verdict v1: "rework needed", scoped): the
  listener-fd hand-off mis-framed early init aborts and lost the honest "setup failed at
  <stage>" verdict (S5-D7, high); the §4.1 standalone load test would hang on a notified
  `mount` (§4.1, med); building the filter in init after `pivot_root` risked 3.14's lazy ctypes
  materialization (S5-D3, med); plus four lows (io_uring denial mechanism, `socketpair`,
  test-skip conflation, `Seccomp: 2` wording). All accepted and applied.

Contract: [`M2-native-warden.md`](M2-native-warden.md) N4 (the whole decision), N5 (seccomp is
the last hard control), N7 (init loads the filter, hands the listener fd to the helper), N11
(the status pipe), §3's chain (stage 12), §9 criteria 3 and 9, §10 step 5. Builds on
[`M2-step3-warden-core.md`](M2-step3-warden-core.md) (the process shape, the verdict, the
status protocol) and [`M2-step4-cgroups.md`](M2-step4-cgroups.md) (the envelope that bounds the
helper's notification work). One commit.

**No pre-plan prototype this time** (Victor's instruction). The step-0 spike already proved the
mechanism end to end on this host (the binding, user notification, the init→helper fd hand-off,
the 256-notification cap, the storm); its scripts are durable in WSL at `~/m2spike/scripts/`
(`sc.py`, `warden.py`). The numbers a fresh prototype would re-gather are written into §3's
build order as named checks instead.

**What this step delivers — the first real `native`.**
- A seccomp **allowlist** built from moby's default profile, loaded by init right before it
  forks the guest, inherited by the guest; default-deny with `EPERM`.
- A **named dangerous subset** that is denied *and reported by name* through seccomp user
  notification, answered by the helper, deduplicated and capped.
- The `seccomp` hard check becomes real: init reports its own `Seccomp:` mode from
  `/proc/self/status`, the server requires `2`, and because the guest is forked after the load
  it inherits the filter. A default `NativeBackend()` run now reports `isolation: native`
  (with step 4's cgroup, and the hard tier) instead of `none`.
- `RunResult.denied_syscalls` is populated for the first time (the field and
  `_format_result`'s `blocked syscalls:` line already exist).

**Not in this step** (Karpathy "simplicity first"; each is a later step's job, or nobody's):
- **Step 6:** selection, the probe, and the test that *every selector-built* native backend
  loads the filter (S3-D9). The test-only bypass survives step 5 and is retired there.
- **Step 7:** the live **foreign-arch kill** test (criterion 9's third bullet — see S5-D4),
  the **helper-CPU-inside-the-envelope** measurement during a storm (criterion 9's second
  bullet; it needs step 4's scope, which only the selector wires in), the README per-distro
  availability table and the `SECURITY.md` seccomp section, and the remaining doc wording
  (golden rule 5's isolation list, `ARCHITECTURE.md`, `PLAN.md`).
- **Left out entirely:**
  - a seccomp on/off config knob — N4/§7: "No knob turns seccomp or any hard control off";
  - a user-editable or per-run profile — one vendored profile, one filter (N4, N10);
  - `SCMP_ACT_LOG`, audit-log integration, or per-syscall arg logging — M3;
  - hand-assembled BPF — libseccomp compiles it (N4, golden rule).

## 1. What the spike and the step-4 code already establish

From the step-0 spike report ([`M2-step0-spike.md`](M2-step0-spike.md)) and its binding script
`~/m2spike/scripts/sc.py`, reproduced on this host (WSL2 Ubuntu 26.04, kernel 6.6.87.2,
libseccomp 2.6.0 / API 7):

| Fact | Result | Matters for |
|---|---|---|
| The ctypes binding over `libseccomp.so.2` | `seccomp_init`, `seccomp_rule_add_array`, `seccomp_attr_set`, `seccomp_load`, `seccomp_notify_*`, `seccomp_syscall_resolve_*` all bind and work from pure stdlib ctypes. No compiler (none on this host). | S5-D3 |
| Building moby's profile for a cap-less amd64 guest | ~309 rules in ~1 ms. 32-bit-only names resolve as unknown on x86_64 and are skipped. | S5-D4, §3 check |
| Load in init, guest inherits | init `seccomp_load`s one filter, forks the guest; the guest shows `Seccomp: 2`. No filter code runs in the guest. | N4, S5-D6 |
| The listener fd hand-off | init sends the notify fd to the helper over the existing init↔helper socketpair with `SCM_RIGHTS`, then closes its own copy. | N7, S5-D7 |
| Denial + naming | `unshare`, `mount`, `ptrace`, `keyctl`, `io_uring_setup`, `bpf`, `socket(AF_VSOCK)` all returned `EPERM` and were reported by name. | S5-D5, criteria 3 and 9 |
| The storm | 50 threads looping `mount` for 2 s. Capped at 256, reporting stopped with the name list deduplicated to `["mount"]`. Uncapped, ~33k notifications/s — so the cap, and the envelope (step 4), are load-bearing. | S5-D5 |
| **Spike Q1 — io_uring** | Node 22 probes `io_uring_setup` at every start: 4 notifications per run. Under N4's reported subset, *every innocent node result* would read `blocked syscalls: io_uring_setup`. | **S5-D2** |
| **Spike Q2 — ptrace** | moby *allows* `ptrace` and `process_vm_readv/writev` on kernels ≥ 4.8; N4's subset denies and reports them. With PID 1 non-dumpable and unreachable, ptrace in the sandbox reaches only the guest's own processes. | **S5-D1 (Victor)** |

From the step-4 code (already on `main`, `8b1de5e`):
- `native_helper.py` `_init` has a `stage = "seccomp"` that **raises** `"the seccomp filter
  arrives in M2 step 5"` unless `cfg["skip_seccomp"]`. That stub is what step 5 replaces.
- `_supervise` (in the helper) polls the pidfd and the init↔helper socket. Step 5 adds the
  notification listener fd to that poll set.
- `native.py` `_verdict` already lists `seccomp` in `_HARD_CHECKS = _INIT_CHECKS + ("seccomp",)`
  and treats every hard check as "`checks.get(name) is True`". The step-4 tests reach `native`
  only through the `seccomp_aside` fixture (which sets `_HARD_CHECKS = _INIT_CHECKS`) plus the
  `_skip_seccomp_for_tests=True` bypass.
- `RunResult.denied_syscalls` exists and renders; nothing populates it yet.

The moby profile (vendored copy at WSL `~/m2spike/moby-default.json`, moby/profiles commit
`6fe7deb`): `defaultAction` `SCMP_ACT_ERRNO`, `defaultErrnoRet` **1 (EPERM)**, 60 syscall
blocks. Block 0 is the 361-name allow list; block 1 is `ptrace`/`process_vm_*` gated on
`minKernel 4.8`; blocks 2–31 are per-family `socket` rules; block 47 is `clone3 → ENOSYS`
(`errnoRet 38`) for the cap-less case; blocks 43–59 are all `CAP_*`-gated (none apply to a
cap-less guest). sha256 `6416b477…3053f6`, 17,867 bytes.

**Assumptions** (Karpathy "think before coding"; each is checked in §3 or made loud at runtime):
- **`libseccomp.so.2` is present.** N4 made seccomp *required for native*, so a host without it
  has no native tier; the helper's import of the binding fails loudly and the run is `none` with
  the reason. `native_skip_reason()` gains the same check (S5-D8), so the Linux gate's native
  tests skip with it rather than erroring.
- **The guest inherits a loaded seccomp filter across `fork` and keeps it across `execve`**
  (with `no_new_privs`, which init already sets). Kernel guarantee; the spike confirmed
  `Seccomp: 2` in the guest.
- **A libseccomp built for the host arch sets a bad-arch action**; moby's archMap is not added,
  so any foreign-arch (32-bit `int 0x80`, x32) syscall is bad-arch. The action is
  `SCMP_ACT_KILL_PROCESS` (S5-D4). *The attribute is checked in a unit test; the live kill is a
  step-7 acceptance item (criterion 9), because triggering a foreign-arch syscall from a
  high-level interpreter needs raw machine code, which this step does not write.*
- **Node's only startup notification is `io_uring_*`** (spike). If the build check in §3 finds
  another innocent notification from a python/bash/node hello, it is added to the silent-deny
  set the same way (S5-D2), and that is recorded — not left to narrow the subset silently.

## 2. Decisions

### S5-D1 (resolved — Victor 2026-10-26; spike Q2) — ptrace policy: **(B) follow Docker**

**Resolved: `PTRACE_POLICY = "allow"`** — `ptrace`, `process_vm_readv`, `process_vm_writev` are
allowed on kernels ≥ 4.8 exactly as moby's profile block 1 does; they leave the notified subset.
Criterion 9 names `mount` (not `ptrace`) as its denied-and-reported example. The reasoning the
decision rested on is kept below.

Moby allows `ptrace`, `process_vm_readv` and `process_vm_writev` on kernels ≥ 4.8 (profile
block 1). N4's named subset would instead **deny and report** them, and criterion 9's draft
tested `ptrace` specifically.

**The two readings, and what each costs:**

- **(A) Keep denying + reporting (stricter than Docker).** `ptrace`, `process_vm_readv`,
  `process_vm_writev` stay in the notified subset → `EPERM`, named in `blocked syscalls:`.
  - *For:* smallest surface; `ptrace` is a recurring ingredient in kernel LPE chains; denying
    is the conservative default and the agent *sees* the denial and can adapt.
  - *Against:* breaks ordinary in-sandbox debugging — `strace`, `gdb`, `py-spy`, a debugger,
    sanitizers, some test runners — for a guest that is only ever tracing *its own* processes.
    Diverges from the container backend (an N9 parity goal): the same snippet behaves
    differently under `native` and `container`.
- **(B) Follow Docker: allow `ptrace` + `process_vm_*` on kernels ≥ 4.8.**
  - *For:* parity with the container backend; debuggers/profilers work; the guest is cap-less,
    `no_new_privs` is set, every mount is `nosuid`, and PID 1 is non-dumpable and outside the
    guest's reach — so `ptrace` here only touches the guest's own descendants, the same bound
    Yama (`ptrace_scope` 1 on this host) already imposes. This is exactly why Docker re-allowed
    it for 4.8+. The `skeptic` review added a second argument: the container backend inherits
    moby's `ptrace` allow, so option (A) would make `native` **stricter than the advertised
    `container` floor on the same host** — a surprising inversion for a user who expects native
    to be a superset.
  - *Against:* a larger surface than (A); a future ptrace-reachable kernel bug would be in
    scope. Criterion 9 must drop `ptrace` as its denied-and-reported example and use another
    (`mount`, `keyctl`, or `unshare` — all still denied+reported under both options).

**Why (B) was recommended and chosen.** Vestibule's stated posture (CLAUDE.md security summary)
is to make the *common, realistic* agent risks structurally impossible, not to defend against a
kernel 0-day; (B) maximizes parity and the real compatibility agents need (debuggers and
profilers are common), and it opens no host path the namespaces + empty caps + `no_new_privs`
don't already close. The `skeptic` review added that (A) would make `native` stricter than the
advertised `container` floor on the same host — a surprising inversion.

**The decision is one constant** `PTRACE_POLICY = "allow"` in `native_seccomp.py` (a compile-time
constant, **not** a config knob). Had it been (A), the same constant would be `"deny"`. It
governs:
- `ptrace`/`process_vm_*` are allowed (as moby's block 1), not in the notified subset;
- criterion 9's denied-example syscall is **`mount`** (§4.2, §9 of the contract);
- one line of the README availability note (step 7).

### S5-D2 (fixed; spike Q1) — `io_uring_*` is denied **silently**

`io_uring_setup`, `io_uring_enter`, `io_uring_register` go in a small **`DENY_SILENT`** set.
A name in `DENY_SILENT` is **excluded from the ported allowlist** (the build loop never adds an
allow rule for it) **and** is **not** in the notified subset. It therefore falls through to the
profile's default action — `SCMP_ACT_ERRNO(EPERM)` — denied exactly as before, just not
*named*, so an innocent node run reports nothing. Docker denies io_uring silently too.
- **The load-bearing mechanism is the exclusion from the allowlist, not an override rule**
  (skeptic): default-deny already returns `EPERM`, so **no explicit `EPERM` rule is added** — a
  second rule for a syscall the allowlist also allows is not guaranteed to win in libseccomp, so
  relying on an override would be fragile. Excluding the name from the allow port is what
  guarantees the denial, regardless of whether moby's block 0 happens to list io_uring in a
  given profile version. It is three names.
- **Checked** in §3: a node hello reports zero blocked syscalls, and a direct `io_uring_setup`
  call returns `EPERM`. If the §3 build check shows another innocent startup notification from a
  python/bash/node hello, it joins `DENY_SILENT` the same way and the §3 note records it — the
  subset is not narrowed silently.

### S5-D3 (fixed; implements N4) — the binding: our own libseccomp ctypes module

- **A new module `src/vestibule/backends/native_seccomp.py`**, stdlib-only ctypes over
  `libseccomp.so.2`. It owns: the `ArgCmp`/`SeccompData`/`Notif`/`NotifResp` structures, the lib
  signatures, `build(profile: dict) -> (ctx, info)`, `load(ctx)`, `notify_fd(ctx) -> int`,
  `alloc() -> (req, resp)`, `receive(fd, req)`, `respond_eperm(fd, resp, id)`,
  `name(nr) -> str`, and the constants (`NOTIFY`, `DENY_SILENT`, `FAMILIES`, `PTRACE_POLICY`,
  the action codes). It is the spike's `sc.py` turned into a real module.
- **Linux-only, like `native_helper.py`:** a `if sys.platform != "linux": raise ImportError`
  guard at the top (so Windows `mypy` reads it), and the `ctypes.CDLL("libseccomp.so.2")` at
  import. **The server (`native.py`) never imports it** — it only reads the vendored JSON with
  plain `json` and passes the parsed dict in the run config. All libseccomp/ctypes stays in the
  helper process, which is always Linux.
- **How the helper gets it (S3-D4/S3-D5 constraints).** The helper runs as `vestibule-warden
  -S -P -c "exec(open(<fd>).read())"` — the package dir is not on its path. The server passes
  `pkg_dir` (the directory of `native_helper.py`) in the run config; the helper, in `_helper`
  **before it forks init** and only when `not cfg["skip_seccomp"]`, does
  `sys.path.insert(0, cfg["pkg_dir"]); import native_seccomp` (with `sys.dont_write_bytecode =
  True` so no `__pycache__` write into a possibly read-only install). The import loads the CDLL;
  init, forked from the helper, inherits both.
- **Build in the helper before the init fork; load in init** (skeptic, S3-D5). The ctx is built
  by the helper right before `os.fork()` and passed into `_init(...)`; init only calls
  `load(ctx)` → `notify_fd(ctx)` → `send_fds(...)` after `pivot_root`.
  - **Why not build in init** (the skeptic's med finding): step 3 found 3.14 materializes
    `ctypes._layout` lazily the first time a `ctypes.Structure` subclass or a
    runtime-created array type (`ArgCmp * n`) or `POINTER(...)` is used — and after
    `pivot_root` the stdlib is out of reach, so that import fails. `build()` creates exactly
    those array types. Running it in the helper, before the fork and before any pivot,
    materializes every ctypes type while the stdlib is still reachable; init then inherits the
    built filter (a C pointer + COW heap) and the materialized types, and its post-pivot
    `load`/`notify_fd`/`send_fds`/`Seccomp:`-readback create **no** new ctypes type. This is
    what the spike did (it built before the fork, loaded in init), so it's the proven path.
  - **Constraint, stated for the build:** no ctypes type in `native_seccomp` may be *first*
    materialized after `pivot_root`. Module-level structs cover the fixed ones; `build()` (run
    pre-fork) covers the array types. Build-order check 2 is a named **3.14** check for this.
  - `no_new_privs` (already set in init) is what lets a cap-less init `load` a filter.
- **The profile travels in the config.** `native.py` reads the vendored
  `seccomp-profile.json` once at module import (a plain file read, cross-platform) and puts the
  parsed dict in the config memfd beside the code (S3-D1). No host path, no file read in the
  helper.

### S5-D4 (fixed; implements N4) — the allowlist, socket families, bad arch, clone3

Built from the profile dict, default-deny (`SCMP_ACT_ERRNO(defaultErrnoRet)` = `EPERM`):
- **Applicable entries only.** An entry applies to a cap-less amd64 guest when it has no
  `includes.caps`, is not excluded on amd64, includes amd64 if it restricts arches, and its
  `minKernel` ≤ this kernel. moby's 17 `CAP_*`-gated blocks (bpf, mount-via-CAP_SYS_ADMIN,
  module load, reboot, …) therefore never enter the filter — the guest has no capabilities, so
  they would be dead rules anyway, and their absence means default-deny covers them.
- **`clone3 → ENOSYS`** (profile block 47): so glibc falls back to `clone`, whose namespace
  flags the filter *can* inspect (moby's `clone` arg rule denies `CLONE_NEW*` for the cap-less
  case). N4's rule; the spike relied on it.
- **Socket families** (N4, which pairs `socket` **and** `socketpair`): moby's own `socket`
  entries are skipped, and `socketpair` is excluded from the ported allowlist; both are replaced
  by four `SCMP_CMP_EQ` allow rules each on arg 0 — `AF_UNIX (1)`, `AF_INET (2)`,
  `AF_INET6 (10)`, `AF_NETLINK (16)`. Every other family (incl. `AF_VSOCK = 40`, **not**
  isolated by the network namespace — contract finding 4) falls through to default `EPERM`;
  `AF_VSOCK` additionally gets a `socket` notify rule so it is *named* (S5-D5).
  - **Why `socketpair` too** (skeptic): N4 names both. moby allows `socketpair` unconditionally
    (no family arg), so without this a cap-less guest could `socketpair` any family. The
    isolation value is small (a socketpair is a local pair, unreachable off-host, and vsock
    doesn't support it), but N4 is explicit, and four extra rules honour it.
- **Bad arch** (N4): moby's `archMap` is not added, so the filter holds only the host arch, and
  `seccomp_attr_set(FLTATR_ACT_BADARCH, SCMP_ACT_KILL_PROCESS)` makes any foreign-arch syscall
  (32-bit `int 0x80`, x32) kill the whole thread group. A 32-bit entry cannot slip past the
  64-bit rules. *Verified at the attribute level in §4.1; the live kill is criterion 9, step 7.*
- **Unknown names** (32-bit-only entries libseccomp can't resolve on amd64) are skipped and
  counted; the §3 check asserts the skipped list is only the expected set.

### S5-D5 (fixed; implements N4) — the notified subset, reporting, and the bounds

- **The notified subset** — denied **and** reported by name, via `SCMP_ACT_NOTIFY`: the mount
  family (`mount`, `umount2`, `pivot_root`, `open_tree`, `move_mount`, `fsopen`, `fsconfig`,
  `fsmount`, `fspick`, `mount_setattr`), `unshare`, `setns`, `bpf`, the keyring (`keyctl`,
  `add_key`, `request_key`), `perf_event_open`, `userfaultfd`, the module calls, `kexec_*`,
  `reboot`, `swapon`/`swapoff`, `open_by_handle_at`, and `AF_VSOCK` via a `socket` arg rule.
  **`ptrace`/`process_vm_*` are allowed, not here (S5-D1 = allow).** `io_uring_*` are **not** here either (S5-D2).
- **The helper answers.** init hands the listener fd to the helper (S5-D7); the helper's
  `_supervise` loop (which already polls the pidfd and the status socket) adds the listener. On
  a notification it `receive`s, resolves the name, records it (first-seen order, deduplicated),
  and `respond`s `EPERM`. No filter code runs in the guest (N4); the helper is outside the
  guest's PID namespace, so no guest can stop it.
- **Bounded** (contract finding 6; spike): after `NOTIFY_CAP = 256` notifications the helper
  **unregisters and closes the listener**; the kernel then fails the rest itself (`ENOSYS`, the
  user-notif default when no listener is attached) with no helper work. The helper's CPU is
  bounded by step 4's envelope regardless. A dedup cap: the name list is naturally small.
- **Reporting is honest, not widened.** N4 asked whether reporting *every* denial stays quiet;
  the spike answered no (node's io_uring), so step 5 keeps the named subset and does **not**
  report arbitrary denials. Innocent python/bash/node runs report nothing (§3 check).
- **Wiring:** the helper sends one status line `{"denied": [names], "notify_stopped": bool}`
  after init's lines, before its own exit (next to step 4's oom line). `native.py` parses it and
  sets `RunResult.denied_syscalls`; if `notify_stopped`, it appends the stderr note
  `[seccomp reporting stopped at the cap; further denied syscalls were not named]` (like the oom
  note; the cap is 256, a constant in the binding — the note need not print it). `denied_syscalls`
  is set for `native` and `native-degraded` alike; it never affects the verdict.

### S5-D6 (fixed; extends S3-D8) — how the server confirms `seccomp`, honestly

- **init reports a kernel fact, not a self-asserted "I succeeded".** After loading the filter,
  init reads `/proc/self/status`'s `Seccomp:` field and returns `seccomp = (mode == 2)` as a
  seventh entry in the `checks` dict — exactly the shape of the existing six init checks
  (`no_new_privs` from `status["NoNewPrivs"]`, `not_dumpable` from `prctl(GET_DUMPABLE)`, …). The
  server already requires every `_HARD_CHECKS` entry to be `True`; `_INIT_CHECKS`/`_HARD_CHECKS`
  are unchanged.
- **What the readback proves, and what it doesn't** (skeptic). `Seccomp: 2` proves *a* filter is
  active, not that it is *the pinned, restrictive* one — a build bug yielding a permissive
  filter would still read `2`. The correctness of the pinned filter rests on the **layered**
  proof, not the readback alone: the sha256 pin (S5-D9), the §4.1 build tests (rule count,
  bad-arch attribute, families, `clone3`), and the §4.2 behavioural denials (`mount`/`unshare`/
  `AF_VSOCK` actually return `EPERM`). The readback's job is only to confirm a filter reached
  the guest at all, closing the "it was never loaded" gap the step-3/4 bypass left open.
- **The guest is the thing we care about**, and it is forked from init *after* the load, so by
  kernel inheritance it has the filter. init's `Seccomp: 2` plus "guest forked after load" is
  why the guest is filtered; the guest need not self-report (it execve's the interpreter), and
  it cannot forge init's status (it is forked after `ready`, and init's status fd is closed to
  it).
- **Stage order in init changes:** load seccomp → read `Seccomp:` → run `_checks` → send
  `ready` → fork guest. (Step 3 ran `_checks` then the seccomp stub; the readback needs the load
  first.) The mount-flag/caps/etc. checks still pass after the load (`statvfs`, `openat`, `read`
  are allowed).
- **The bypass path is now driven by the readback, not a raise.** With
  `_skip_seccomp_for_tests=True` init loads no filter, `Seccomp:` is `0`, `checks["seccomp"]` is
  `False`, and the verdict is `none` — "hard controls not confirmed: seccomp", the same label
  step 3 produced, now from a real kernel readback. init no longer raises at this stage.

### S5-D7 (fixed; implements N7) — the listener-fd wire protocol

The v1 draft bound the hand-off to a flag (`cfg["skip_seccomp"]`) and had the helper `recv_fds`
unconditionally on the filter path. The skeptic showed that breaks every init abort *before* the
seccomp stage: step-3 init sends `{"error":"mounts",...}` (a bad rootfs, a restricted userns,
an old kernel) with **no** control message, and a helper that `recv_fds` first would eat the
leading `{`, mis-frame the line, and the honest "setup failed at mounts" verdict would be lost
— replaced by the weaker "no status report". v2 removes the coupling:

- **The helper's *first* read of the init socket is always `recv_fds(65536, 1)`**, whatever the
  path:
  - **An fd came with it** → it is the listener; the one accompanying byte (`b"L"`) is the tag
    and is discarded; the helper registers the listener in its `poll` set and starts answering
    notifications (S5-D5). (SCM_RIGHTS on a stream socket: the kernel ends the `recvmsg` at the
    control-message boundary, so this read returns only the `b"L"` byte, never the following
    `ready` bytes.)
  - **No fd came with it** → the bytes *are* the first chunk of the status stream (a `ready`
    line on the bypass path, or an `error` line from an early abort) and are fed straight into
    the relay. Nothing is discarded, so the early-abort error survives.
  - **Empty data and no fd** → init died before sending anything; EOF, as today.
  Every later socket read uses plain `recv`, as now.
- **init sends the listener fd only after a successful `load`** (`send_fds(sock, [b"L"], [lfd])`,
  then closes its own copy — N7). On any abort before that point it has sent only status bytes;
  on the bypass path it never loads and never sends a control message. **Neither side keys off a
  flag** — the presence or absence of an fd on that first read is self-describing, so the stream
  can't mis-frame in either path.
- init closes its listener copy immediately (N7), so once the helper exits nothing holds it.
- **Regression test:** an empty/invalid rootfs on the real (filter) `backend` still reports
  `none`, "native sandbox setup failed at mounts: …" (§4.2) — the v1 draft would have lost it.

### S5-D8 (fixed) — how the tests reach the real filter

- **`native_skip_reason()` gains a libseccomp check** (`ctypes.CDLL("libseccomp.so.2")`): native
  now requires it, so native-marked tests skip with that reason where it's missing rather than
  erroring. The Linux gate (0 skips) therefore asserts libseccomp is present in WSL.
- **The `backend` fixture flips to the real filter:** `NativeBackend()` (no bypass). The step-3
  behavioural tests that asserted `STEP3_LABEL`/`none` as a "the sandbox ran this far" proxy now
  assert the real verdict. The step-4 cgroup tests that reached `native`/`native-degraded`
  through `seccomp_aside` **drop `seccomp_aside`** and keep their assertions — the real filter
  now supplies the `seccomp` check the fixture used to fake. Every changed assertion traces to
  one cause: seccomp now loads by default (Karpathy surgical).
- **A behavioural test asserts the filter, not the whole stack** (skeptic). A host with
  userns + libseccomp + rootfs but **no systemd** gives `native-degraded` (placement returns
  `None`), so a bare `== "native"` on a `native`-marked (not `@cgroups_required`) test would
  *fail* instead of *skip*, conflating the two skip reasons. So:
  - tests whose point is "the filter loaded and the hard tier holds" (jail, guest-view, the
    denial runs) assert `isolation in {"native", "native-degraded"}`;
  - tests that assert exactly `native` (the milestone hello) are marked `@cgroups_required`, so
    they skip cleanly where systemd is absent. The Linux gate host has systemd, so it runs them.
- **The bypass and `seccomp_aside` survive, narrowed to their real jobs:** one test proves
  `NativeBackend()` → `native` versus `NativeBackend(_skip_seccomp_for_tests=True)` → `none`
  "…not confirmed: seccomp"; `seccomp_aside` stays only where a test must isolate a soft-limit
  path without depending on the filter (e.g. the rejected-cpu-limit test keeps its meaning if
  ever run on a libseccomp-less host — but it is `native`-marked, so in practice the filter is
  there). Step 6 retires the bypass with the "every selector-built backend loads the filter"
  test (S3-D9).

### S5-D9 (fixed; implements N4) — vendoring, attribution, pinning

- **The profile is vendored** at `src/vestibule/backends/seccomp-profile.json`, the verbatim
  moby/profiles file at commit `6fe7deb`. Hatchling ships every file under `src/vestibule`, so
  no packaging config is needed (verified by the sha256 test below running against the source
  tree; a wheel-contents check is step 7's packaging item).
- **Attribution** (Apache-2.0; Vestibule is MIT): `native_seccomp.py`'s module docstring names
  the source (`moby/profiles@6fe7deb`), the licence, and that the allowlist is *derived* from
  it. A one-line `SECURITY.md` note is step 7.
- **Pinned by a test:** `test_docs.py` (or a small `test_seccomp_profile.py`) asserts the
  vendored file's sha256 == `6416b477…3053f6` and its size == 17,867 bytes, so a silent re-vendor
  is caught and must be a deliberate, reviewed change (like M1's image re-pin).

### S5-D10 (fixed; scoped doc/config changes) — only what step 5 makes false

- **`pyproject.toml`:** remove the `seccomp = ["pyseccomp>=0.1.2"]` extra (N4: "The `seccomp`
  extra leaves `pyproject.toml`"). Nothing references `pyseccomp`; leaving a dead extra pointing
  at an abandoned dependency misleads. No new marker is needed (`native` already covers it;
  S5-D8 folds libseccomp into its skip reason).
- **`CLAUDE.md`:** the two bullets step 5 directly falsifies — the Isolation tech bullet
  ("seccomp uses the optional `pyseccomp` binding") and "Don't hand-roll seccomp BPF. Use
  `pyseccomp`, keep it optional" → our own `libseccomp` ctypes binding, a Docker-derived
  allowlist, **required for native**; still never hand-assemble BPF (libseccomp compiles it).
- **Deferred to step 7's doc bundle** (kept out of this commit, Karpathy surgical): golden rule
  5's isolation list (`namespaces-only` is retired by N5 and was already wrong before step 5),
  the one-paragraph "optional seccomp" in CLAUDE.md's intro, `docs/PLAN.md`'s seccomp rows, and
  `ARCHITECTURE.md` §8 corrections. None of those is about step 5's mechanism.

## 3. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/native_seccomp.py` | new: the ctypes binding, `build`/`load`/`notify_fd`/`alloc`/`receive`/`respond_eperm`/`name`, the subset/family/policy constants, attribution (S5-D3–S5-D5, S5-D9) |
| `src/vestibule/backends/seccomp-profile.json` | new: vendored moby profile `6fe7deb` (S5-D9) |
| `src/vestibule/backends/native_helper.py` | import + `build()` the ctx in the helper before the init fork (S5-D3); replace init's seccomp stub with `load`→`notify_fd`→`send_fds` and the `Seccomp:` readback (S5-D6); the first-read `recv_fds` + listener in `_supervise`, dedup+cap+close, the `denied` status line (S5-D5, S5-D7) |
| `src/vestibule/backends/native.py` | read + pass the profile dict and `pkg_dir` in the config; parse the `denied` line into `RunResult.denied_syscalls` + the "reporting stopped" note (S5-D5); the vendored-profile module constant |
| `tests/nativeutil.py` | the libseccomp check in `native_skip_reason()` (S5-D8) |
| `tests/test_native_seccomp.py` | new: §4.1's binding tests (the one place the binding is unit-tested on Linux) |
| `tests/test_native.py` | the `backend` fixture → real filter; migrate the step-3/4 assertions (S5-D8); §4.2's new seccomp runs; the default-vs-bypass test |
| `tests/test_native_verdict.py` | the `seccomp`-from-readback rows (S5-D6) |
| `tests/test_docs.py` (or `test_seccomp_profile.py`) | the sha256 + size pin (S5-D9) |
| `pyproject.toml` | drop the `seccomp` extra (S5-D10) |
| `CLAUDE.md` | the two seccomp-mechanism bullets (S5-D10) |
| `docs/GETTING_STARTED.md` | the native-tests bullet: native now needs `libseccomp.so.2` |
| `docs/plans/M2/M2-native-warden.md` | amendment blockquotes: N4 (Q1/Q2 resolved), §3 stage 12, the status protocol |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | the sign-off bullet, the step-5 entry |

**The run, as built** (contract §3, changed lines marked ▲):

```
helper: PDEATHSIG; wait for release; ▲ (if seccomp) sys.path.insert + import native_seccomp
        ▲ (if seccomp) ctx = build(profile)   ← before the fork, so no ctypes type is first
        unshare(USER|PID); maps;                 materialized after init's pivot_root (3.14)
        fork init(…, ctx); init → guest/sandbox/cgroup.procs; "go";
        ▲ first socket read is recv_fds: fd → listener (discard "L" tag); no fd → relay bytes
        relay; ▲ supervise also answers the listener (EPERM + record, dedup, cap 256, close);
        wait; oom line; ▲ denied line
init:   read /proc/self/cgroup; setsid; unshare(NS|NET|UTS|IPC|CGROUP); jail; privileges;
        RLIMIT_NPROC?;
        ▲ stage seccomp: load(ctx) → notify_fd → send_fds([b"L"],[lfd]) → close   (no new type)
        ▲ stage checks: _checks now reads Seccomp: from /proc/self/status (== 2)
        ready (with the 7th check); fork guest (inherits the filter); reap; exit line
server: collect; verdict (seccomp now real); ▲ denied_syscalls from the helper's line; OOM note
```
(An abort before the seccomp stage sends only an `error` line and no control message; the
helper's first `recv_fds` returns no fd and relays those bytes, so the honest verdict survives —
S5-D7.)

**Size check** (Karpathy, step 3's measure — code lines, not blank/comment/docstring):
- **`native_seccomp.py`:** ~140 (the spike's `sc.py` is ~130 incl. `build`; add `alloc`/
  `receive`/`respond_eperm` wrappers and the policy constant, drop the spike's `info`
  bookkeeping that only the spike printed). Look again past ~170.
- **`native_helper.py`:** 329 now → ~365. The stub (2 lines) becomes the import + pre-fork
  `build()` (~5), init's load/`notify_fd`/hand-off (~8), the readback (~2), and the first-read
  `recv_fds` + listener arm of `_supervise` + the `denied` line (~22). Look again past ~380; an
  overrun is recorded before the step is done (step 2's lesson) — if `_supervise`'s three-fd
  loop pushes it past, the notification serve loop moves into `native_seccomp.serve(listener,
  cap)` and the helper just calls it.
- **`native.py`:** 428 now → ~445 (read/pass the profile; parse one status line). Look again
  past ~460.

**Build order, each step with its check (the measurements a prototype would have gathered):**
1. `native_seccomp.py` + the vendored JSON; `build()` only. → §4.1: builds to a rule count in
   `[330, 360]` (347 here); the unknown-name list is exactly the expected 32-bit/foreign-arch
   set; bad-arch attribute is `KILL_PROCESS`; only the host arch is registered; sha256/size pin.
   `mypy src` clean on both platforms. **(Done: 347 on 3.14 and 3.11, bad-arch=KILL, x86/x32
   absent.)**
2. helper builds the ctx pre-fork; init load→readback; `_checks` gains `seccomp`; the stub is
   gone. → §4.2 default run of python/bash/node → `isolation: native`, exit 0, **zero**
   `blocked syscalls`; bypass → `none` "…not confirmed: seccomp". **Named 3.14 check** (skeptic):
   this run must reach `native` on the gate's 3.14 interpreter, not just 3.11 — proving no
   ctypes type is first materialized after `pivot_root`. The gate runs `test_native.py` on both.
3. The listener hand-off + `_supervise` arm + the `denied` line + `native.py` parsing. → §4.2:
   `mount`, `unshare`, `keyctl`, `AF_VSOCK` each → `EPERM` and named; a `socket(AF_INET)`
   succeeds; `io_uring_setup` → `EPERM`, **not** named (S5-D2).
4. The cap + dedup + listener close. → §4.2 storm: a guest with many threads looping `mount`
   returns, `denied_syscalls == ["mount"]`, the "reporting stopped" note present, within the
   timeout.
5. S5-D1's `PTRACE_POLICY = "allow"` wired. → §4.2 ptrace test: a guest tracing its own child succeeds; `ptrace` is not in `denied_syscalls`.
6. Migrate the step-3/4 tests off the bypass/`seccomp_aside` (S5-D8); `native_skip_reason`
   libseccomp check. → the whole `test_native.py`/`test_native_verdict.py` suite green.
7. Mutation checks (§4.3) → each guarding test turns red.
8. Both platform gates green; **0 skips on Linux**, inside the 540 s budget; the time recorded.
   - **Expected cost:** the storm test (~a few s) and the extra real runs add ~15 s to each
     pytest run that includes `test_native.py` (two do). The gate was 207 s after step 4, so
     ~225 s. Past 250 s, the slow tests are looked at first.
9. The size check, then docs → `tests/test_docs.py` passes.

## 4. Tests

### 4.1 All platforms? No — the binding is Linux-only (`test_native_seccomp.py`, `native` marker)

The binding calls `libseccomp`, so these are `native`-marked (they need libseccomp, folded into
the skip reason). They do **not** need the rootfs or user namespaces — they build and probe a
filter in-process — but the marker is simplest and honest.
- **`build()` from the vendored profile:** `info["rules"]` (total rules added — profile ports
  plus the 8 family and 28 notify rules) is **347** on this host's libseccomp, deterministic
  across 3.14 and 3.11; the test asserts a `[330, 360]` band. `info["unknown"]` is exactly the
  expected 32-bit/foreign-arch names; no `CAP_*`-gated name is ported as an *allow* (they appear
  only as notify rules where they're in the dangerous subset).
- **Bad arch:** `seccomp_attr_get(FLTATR_ACT_BADARCH)` == `SCMP_ACT_KILL_PROCESS`; the filter
  holds only the host arch (adding none from `archMap`).
- **Socket families:** `socket` **and `socketpair`** each have the four allowed-family allow
  rules; `AF_VSOCK` has a `socket` notify rule; no other family rule exists (so they
  default-deny).
- **`clone3`:** resolves to an `ENOSYS` errno rule, not allow.
- **S5-D2:** `io_uring_setup/enter/register` are in `DENY_SILENT`, are **excluded from the
  allowlist** (no allow rule for them), and are not in the notified subset — so they default-deny
  with no explicit override rule.
- **S5-D1 (`allow`):** `ptrace`/`process_vm_readv`/`process_vm_writev` have allow rules and are
  not in the notified subset.
- **`name(nr)`:** a known number round-trips to its name; an absurd number returns `#<n>`.
- **The pin:** sha256 and byte size of `seccomp-profile.json`.

A separate Linux test loads the built filter in a *throwaway child* and asserts the child then
gets `EPERM` on a **silent-deny** syscall — `io_uring_setup` (skeptic). It must **not** use a
notified syscall like `mount`: a lone child that triggers a NOTIFY rule with its listener fd
open but unserviced blocks in-kernel and would hang to the pytest timeout. KILL stays an
attribute-level assertion only (above); a live foreign-arch kill needs raw machine code and is
criterion 9, step 7. (The child forks, loads, tries one silent-deny call; it never touches the
host beyond that child.)

### 4.2 Linux, `native` marker (real sandboxes)

Each uses a `tmp_path` workspace and the step-3/4 fixtures (now real-filter); the step-4
scope-teardown check still runs.
- **Hello world, the milestone** (`@cgroups_required`): python, bash and node print, exit 0,
  `isolation: native` and **no** `blocked syscalls`. The first real `native`.
- **Default vs bypass:** `NativeBackend()` → `native` or `native-degraded` (filter loaded);
  `NativeBackend(_skip_seccomp_for_tests=True)` → `none`, "hard controls not confirmed: seccomp".
- **Setup-failure framing survives the hand-off** (S5-D7 regression, high finding): an
  empty/invalid rootfs on the real (filter) `backend` reports `none`, "native sandbox setup
  failed at mounts: …; nothing was executed" — not "no status report". The v1 wire protocol
  would have lost this.
- **Denied + named** (criteria 3, 9): a python guest that tries `mount`, `unshare`, `keyctl`,
  and `socket(AF_VSOCK)` → each `EPERM`, each in `denied_syscalls`; the verdict is
  `native`/`native-degraded`. `socket(AF_INET)` / `AF_UNIX` and an `AF_UNIX` `socketpair`
  succeed; an `AF_VSOCK`/`AF_PACKET` `socketpair` fails. A guest `ptrace`-ing its own child
  succeeds and is **not** named (S5-D1 = allow); `mount` is the denied-and-named example.
- **io_uring silent** (S5-D2): a guest calling `io_uring_setup` gets `EPERM`, and
  `denied_syscalls` is empty; a node hello's `denied_syscalls` is empty.
- **The storm** (criterion 9; the envelope-CPU half is step 7): a guest with N threads looping
  `mount` returns within the timeout; `denied_syscalls == ["mount"]`; the "reporting stopped at
  the cap" note is present; the next run is clean.
- **AF_VSOCK** (criterion 3): `socket(AF_VSOCK, …)` → `EPERM`, named — the vsock hole N4 called
  out is closed by the filter, not the netns.
- **Network still gone:** the step-3 network test still passes (TCP/UDP/DNS fail) — the filter
  allows the four families but the empty netns gives nothing to reach.
- **Seccomp survives the guest's own `execve`:** a python guest that `os.execv`s a second
  interpreter still can't `mount` (the filter persists; `no_new_privs`).

### 4.3 Mutation checks (during the build; not committed)

Each mutant must turn its guarding test red:
- bad-arch action left `ALLOW`/default → the bad-arch attribute test;
- `archMap` arches added to the filter → the single-arch test;
- socket allow-all (no family arg) → the `AF_VSOCK`-denied run;
- a family dropped from `FAMILIES` → the `AF_INET` run (it would fail);
- `socketpair` left in the ported allowlist (no family rule) → the `AF_VSOCK`-socketpair run;
- `io_uring` left in the notified subset → the node-hello "zero blocked syscalls" test;
- `io_uring` left in the allowlist (not excluded) → the `io_uring_setup` → `EPERM` test;
- `clone3` allowed instead of `ENOSYS` → the `clone3` binding test;
- the ctx built in init (after pivot) instead of the helper pre-fork → the §4.2 default-run
  `native` test on **3.14** (would abort post-pivot with a ctypes import error → `none`);
- the helper's first socket read uses plain `recv` not `recv_fds` → the denied-and-named run
  (no listener → no notification) **and** the empty-rootfs framing regression (the error line's
  leading byte would be eaten → "no status report" instead of "setup failed at mounts");
- seccomp never loaded (stub returns) → the default-run `native` test (would be `none`);
- init reports `seccomp` without reading `/proc/self/status` (hard-codes `True`) → the bypass
  test (would wrongly be `native`);
- stage order unchanged (checks before load) → the readback test (`Seccomp` would be 0);
- no listener hand-off → the denied-and-named test (no notification arrives);
- the helper responds `ALLOW` instead of `EPERM` → the denied-and-named test (the call
  succeeds);
- no dedup → the storm test (the name list is huge);
- no cap / listener never closed → the storm test (no "reporting stopped", or it runs long);
- `denied` line not parsed → the denied-and-named test;
- `PTRACE_POLICY` set to `deny` → the ptrace test (S5-D1): tracing a child would be denied.

### 4.4 Done when

- **Gates:** both platform gates green; on Linux, 0 skips, inside the 540 s budget.
- **Size:** §3's check, any overrun recorded.
- **Review:** `critic` and `verifier` leave no open CONFIRMED high/medium findings.
- **Docs:** the contract amendments, the GETTING_STARTED bullet, a HISTORY bullet, a BUILD_LOG
  entry.

## 5. Edge cases

- **No `libseccomp.so.2` at runtime** (not a test host): the helper's import fails → `main`'s
  try/except sends an `error` line → verdict `none`, "native sandbox setup failed at …; nothing
  was executed". Step 6's selection then falls through to the container backend (N4/N5: seccomp
  is a hard control). For step 5's direct tests, `native_skip_reason` skips instead.
- **A notification arrives after the listener is closed** (past the cap): the kernel returns
  `ENOSYS` to the guest itself, no helper involvement (spike). The run is unaffected; the note
  already says reporting stopped.
- **The guest triggers a notified call during the window between init's load and the helper
  registering the listener:** the kernel queues the notification on the listener fd; the helper
  reads it when it polls. Nothing is lost. (init forks the guest only after `ready`, by which
  point the helper has the fd.)
- **init itself makes no notified call** after the load (its remaining syscalls — `read`,
  `write`, `clone`, `wait4`, `close`, `exit` — are all allowed), so init never self-blocks on
  the notification it would otherwise have to answer for itself.
- **A denied call in the mount family during jail build:** can't happen — the jail is built
  *before* the filter is loaded (init stages: jail → privileges → seccomp). The filter only ever
  constrains the guest (and init's trivial tail).
- **`send_fds`/`recv_fds` framing, all paths** (S5-D7): the helper's first socket read is
  always `recv_fds`; an fd means the listener (discard the `b"L"` tag), no fd means those bytes
  are the first status chunk and are relayed. So the filter path (fd then `ready`), the bypass
  path (`ready`, no fd), and an **early abort** (`error`, no fd — the case the v1 draft lost)
  all frame correctly. init sends the fd only after a successful `load`.
- **The storm under step 4's envelope:** the helper's answering work is charged to
  `supervisor/`, inside the scope's `MemoryMax`/`CPUQuotaPerSecUSec`, so a guest can't make the
  helper exhaust the host (N3). The envelope-CPU assertion is step 7 (it needs the selector's
  scope wiring).
- **The stalled-stderr known issue** (BUILD_LOG): the notification loop logs nothing on the hot
  path; the `denied` line goes on the status pipe, not stderr.
- **A profile re-vendor** (moby ships a new `6fe7deb+`): the sha256 test fails, forcing a
  deliberate re-pin (M1's image re-pin discipline).

## 6. Carried forward

- **Step 6:** native becomes reachable from the server; the probe confirms the filter loads on a
  real run; the test that *every selector-built* native backend loads the filter (S3-D9), which
  retires the bypass; the "No libseccomp → container fallback" message (contract §5).
- **Step 7:**
  - **criterion 9's live foreign-arch kill** — needs a safe trigger (raw machine code, not
    written here); if none is found, it is brought to Victor, with the attribute-level proof
    (§4.1) standing in;
  - **criterion 9's envelope-CPU-during-a-storm** measurement (step 4's scope + step 5's storm);
  - the README per-distro availability table (incl. the ptrace line from S5-D1) and the
    `SECURITY.md` seccomp section (the allowlist, the vsock note, the guest-symlink residual
    N4 kept);
  - the remaining doc wording (golden rule 5's isolation list, `ARCHITECTURE.md` §8,
    `PLAN.md`); the wheel-contents check for the vendored JSON.

## 7. Review record

`skeptic` reviewed draft v1 on 2026-10-26. Verdict: **"rework needed"**, scoped to three issues
(the decision structure — allowlist derivation, notified subset, io_uring silent-deny, bad-arch,
the `Seccomp: 2` verdict — was found sound). S5-D1 (ptrace) was presented as two options; the
review endorsed the recommendation (B) and added the "native would be stricter than container"
argument (now in S5-D1). Every finding was accepted and applied in v2.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | high | The listener-fd hand-off bound to the seccomp stage mis-frames every init abort *before* it (bad rootfs → `error:"mounts"` with no control message), losing the honest "setup failed at <stage>" verdict | Accepted: the helper's first socket read is always `recv_fds`; fd → listener, no fd → relay the bytes; init sends the fd only after `load`; no flag coupling (S5-D7). A regression test (§4.2) and a mutant (§4.3) |
| 2 | med | §4.1's "load in a throwaway child" test asserts `EPERM` on `mount` — a notified syscall with an unserviced listener blocks in-kernel, so the child hangs; "KILL is configured" is untriggerable without machine code | Accepted: assert `EPERM` on a silent-deny syscall (`io_uring_setup`); KILL stays attribute-level (§4.1) |
| 3 | med | Building the filter in init after `pivot_root` risks 3.14's lazy `ctypes._layout` materialization (step 3's own trap), and the plan didn't state the constraint | Accepted: build in the helper before the init fork; init only `load`/`notify_fd`/`send_fds` post-pivot (no new ctypes type); the constraint stated; build-order check 2 is a named 3.14 check (S5-D3) |
| 4 | low-med | Behavioural tests asserting `== "native"` under the `native` marker (not `@cgroups_required`) would fail, not skip, on a systemd-less host — conflating the skip reasons | Accepted: those assert `native`/`native-degraded`; the exact-`native` milestone is `@cgroups_required` (S5-D8) |
| 5 | low | S5-D2's rationale was wrong: the denial comes from excluding io_uring from the allowlist, not an override rule (a conflicting rule isn't guaranteed to win) | Accepted: no explicit rule; exclusion from the allowlist is the mechanism, reworded (S5-D2) |
| 6 | low | N4 pairs `socket` and `socketpair`; the plan restricted only `socket` | Accepted: the family restriction extends to `socketpair` (S5-D4, §4.1) |
| 7 | low | "init reports a kernel fact, not a self-grade" overstates `Seccomp: 2` — it proves a filter is active, not that it is the pinned one | Accepted: the layered proof (pin + build tests + behavioural denials) stated; the readback's narrower job clarified (S5-D6) |

Sound items the review confirmed: the bad-arch deferral is honest (the claim rests on the
attribute + single-arch, both unit-tested; the live kill is confirmation, legitimately step 7);
init's filtered reap/`_send` tail is safe (all its post-load syscalls are allowed, none
notified); and "init grading its own homework" is consistent with the existing six trusted init
booleans (the guest can't forge the status).

## 8. As built (2026-10-27)

- **The mechanism was verified on both gate interpreters before the tests** (build-order checks,
  run directly against the real trees in WSL, 3.14 and 3.11, identical results): the filter
  builds to 347 rules with bad-arch = `KILL_PROCESS` and only the host arch; a default python/
  bash/node run → `isolation: native`, exit 0, zero blocked syscalls; the bypass → `none`
  "hard controls not confirmed: seccomp"; `mount`/`unshare`/`keyctl`/`AF_VSOCK` → `EPERM` and
  named (`vsock` by its `socket` syscall), `io_uring_setup` → `EPERM` silent, `AF_INET` and a
  self-`ptrace` allowed; a 60k-notification storm returns with `denied == ["mount"]` and the
  "reporting stopped at the cap" note, in ~2.3 s.
- **Sizes** (code lines, the step-3/4 measure):
  - `native_seccomp.py`: **175** (estimate ~140, look-again ~170). Looked at: ~15 lines are
    irreducible ctypes signature boilerplate (`_lib.X.argtypes`/`restype`), ~9 are the
    test-support helpers (`attr_get`, `arch_exists`, `release`) the §4.1 binding tests need, and
    `build` factors into `_action`/`_add`. The estimate undercounted the boilerplate; nothing is
    bloat, so it stands.
  - `native_helper.py`: **382** (estimate ~365, look-again ~380). Two lines over. Looked at: the
    plan's fallback was to move the notification serve loop into `native_seccomp.serve()`, but
    that loop shares `_supervise`'s poller, deadline and first-read framing (the S5-D7 fix), so
    extracting ~10 lines would split one coherent loop across two files and push `native_seccomp`
    further over *its* line. Kept; every step-5 addition is load-bearing.
  - `native.py`: **439** (estimate ~445, look-again ~460). Within estimate.
- **Departures from the plan's wording:**
  - the "reporting stopped" stderr note carries no number (the cap is a binding constant; the
    server can't import the Linux-only module, so printing the literal would duplicate it);
  - `info["allowed"]` in the plan is `info["rules"]` in the code (it counts notify + family
    rules too, so "allowed" was a misnomer); the §4.1 band is `[330, 360]` (347 here);
  - `test_what_step_4_can_claim` is renamed `test_a_full_run_claims_native` (step 5 claims it
    for real, no longer "what step 4 can claim with the check set aside").
- **The S5-D7 regression is covered by `test_an_empty_tree_fails_setup_at_mounts`** running on
  the real-filter `backend` fixture: init aborts at the mounts stage, sends an `error` line and
  no listener fd, and the helper's first `recv_fds` relays those bytes — so the honest "setup
  failed at mounts" survives, which the v1 draft would have turned into "no status report".
