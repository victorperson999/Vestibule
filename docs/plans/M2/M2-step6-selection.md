# M2 step 6 — Selection on Linux: native becomes reachable

Status: **v2, signed off and built 2026-10-27 (§8), after the `skeptic` review and two
`critic` rounds, each checked by `verifier` (§7). Victor decided both open questions, each as
recommended:**
- **S6-D1:** the native probe relies on the per-run verdict (amends contract §5);
- **S6-D10:** the bypass is deleted, not kept.

Everything else is fixed: one defensible answer each, most of them already decided by the
contract or an earlier step.

Contract: [`M2-native-warden.md`](M2-native-warden.md) N5 (tiers, labels, order), §4.1 (one
shared concurrency budget), §5 (selection and probing on Linux), §7 (`select.py`, config),
§10 step 6, and criteria 11, 13 and 17. Carried forward here from:
- step 1b ([`M2-step1-linux-baseline.md`](M2-step1-linux-baseline.md) S1-D5): native and
  `cleanup_job()`;
- step 2 ([`M2-step2-rootfs.md`](M2-step2-rootfs.md) S2-D4, S2-D9, §6): the "digest reference"
  message, the overlap refusal, the exact `setup` command;
- step 3 ([`M2-step3-warden-core.md`](M2-step3-warden-core.md) S3-D9, §5, §6): the filter can't
  be skipped from the server; the message for a `/proc` that isn't fully visible;
- step 4 ([`M2-step4-cgroups.md`](M2-step4-cgroups.md) §6): the probe's placement outcome and
  time; the reason limits are unavailable; criterion 17;
- step 5 ([`M2-step5-seccomp.md`](M2-step5-seccomp.md) §6): the filter on a real selected run;
  the "no libseccomp" message.

One commit.

**What this step delivers:** a Linux user who ran `vestibule-mcp setup` gets
`isolation: native` from the MCP server.
- `VESTIBULE_BACKEND` takes `auto | native | container | naive`.
- On Linux, `auto` probes native first and follows N5's order. Off Linux, nothing changes.
- Native runs share the selector's one concurrency budget.
- Every reason native is unavailable or degraded gets a message that names the cause, and the
  fix where there is one.

**Not in this step** (Karpathy "simplicity first"):
- **Step 7:** the README's Linux section and per-distro table, the benchmark, the
  `ARCHITECTURE.md`/`PLAN.md`/`CLAUDE.md` wording (golden rule 5's isolation list), the
  GETTING_STARTED Linux section, the live-agent check (E2), and criterion 9's live
  foreign-arch kill and envelope-CPU measurement.
- **Left out entirely:**
  - switching to native in a server that already chose the container, after `setup` runs
    (S5-D1 caches a success; the log says to restart);
  - a setting for the order;
  - a direct D-Bus client.

## 1. What the code already gives us

| Fact | Where | Matters for |
|---|---|---|
| Selection is lazy, lock-guarded and cached. A failure is re-checked after 30 s (S5-D1). A failure only a restart can fix is `_RestartRequired` and never suggests waiting. | `select.py` `get()` | unchanged |
| M1's container flow: resolve the runtime, probe the full profile, retry once with every soft limit off (S5-D2), else refuse. The unknown-`VESTIBULE_RUNTIME` check lives inside it. | `select.py` `_select()`, `_resolve_runtime()` | S6-D3 |
| `VESTIBULE_BACKEND` is checked by the selector, not by config: `auto` and `naive` pass, anything else is restart-required. | `select.py`, `config.py` | S6-D3 |
| One `asyncio.Semaphore(max_concurrent)` shared by every container backend (S7-D1), asserted by `test_reselection_shares_one_slot_budget`. `NativeBackend` makes its own when it has none. | `select.py`, `native.py:104-106`, `test_select.py:439-458` | S6-D5 |
| `note_result` drops the cache when the live selection's own warden returns `none` and the selection wasn't `none`. | `select.py` | S6-D6 |
| `cleanup_job()` (the POSIX hand-off) and `aclose()` (Windows) walk the tracked container backends. | `select.py`, `server.py` | S6-D2 |
| Every native run is verified by the server from facts init reports from the kernel: the namespace inodes, the identity maps, the mount table and flags, the capability sets, `NoNewPrivs`, a non-dumpable PID 1, only `lo`, no terminal, `Seccomp: 2`, the guest limits read back. | `native.py` `_verdict` (steps 3–5) | S6-D1 |
| **That verdict sees nothing the guest does after `ready`.** A guest that can't start (exit 127, "cannot start …" on stderr) or that times out still gets `native` with no detail. | `native.py:450-489`, `native_helper.py:417-429` | S6-D4 |
| A native run creates no host files (S3-D1). When the server exits, PDEATHSIG ends the helper, init and the namespace, and systemd removes the scope. | `native_helper.py` | S6-D2 |
| `native_cgroup.place` logs why placement failed, then returns `None`; the run's detail only says "limits not applied: cpu, memory, pids". | `native_cgroup.py` | S6-D7 |
| `rootfs.find` returns `None` for an unpinned ref (it swallows `registry.parse_ref`'s "not pinned: native needs a digest reference" error) and for an unreadable cache. `rootfs.overlap_problem` exists. No helper builds the `setup` command line. | `rootfs.py:50-75` | S6-D4 |
| M1's probe scripts run unchanged in a native sandbox; their cgroup readback prints `cgroup=none` (no `/sys`) and nothing reads it. | `select.py` | S6-D1 |
| Tests that reach the real selector with `backend=auto` and expect the container: `test_select.py` (its `_limits()` helper), `test_container.py` (four selection tests; one reads `sel.warden._owner`, one matches "cannot enforce"), `test_acceptance.py`, `test_stdio_e2e.py` (the `server` fixture, and the SDK-client test's own env dict). `stdioutil.py`'s spawn regex only matches the container's log line. | `tests/` | S6-D8 |
| The `run_code` description says "an isolated sandbox", true of both backends. | `server.py` | no change |
| `select.py` is 328 code lines (step 5's measure). | | §3 |

**Assumptions** (each checked in §3 or said at runtime):
- **The gate host has a systemd user manager**, so native is full there. A host without one
  gets `native-degraded`, handled by N5's order.
- **Both rootfs trees are needed.** The probe runs `bash` in the python tree; node runs use the
  node tree. Selection requires both, but doesn't run node.
- **On stock Ubuntu with AppArmor's user-namespace restriction**, the helper's own `setgroups`
  or `uid_map` write fails first (stage `helper`), by the code order and the widely reported
  `unshare -r` symptom. This host can't show it (contract N5); M4's VM run confirms it.

## 2. Decisions

### S6-D1 (Victor, 2026-10-27; amends contract §5) — The native probe is a normal run plus the workspace round-trip

§5 says the probe prints its uid, gid and groups, the `Cap*`, `NoNewPrivs` and `Seccomp` lines,
its open fds, its network interfaces and `/proc/self/mountinfo`, tries `/proc/1/environ`,
writes a byte to `/tmp`, and "the server checks it all".

**Decided (as recommended):** the native probe runs M1's `bash` workspace round-trip (`_PROBE_RW`, or
`_PROBE_RO` in read-only mode) through the normal native path. It earns a native candidate only
when the guest exits 0, doesn't time out, prints the marker, and the run's own isolation is
`native` or `native-degraded`.

**Why:**
- **Steps 3–5 moved every check §5 lists into every run.** The server judges them from facts
  init reads from the kernel before the guest exists. A fact the guest prints is weaker: the
  untrusted side reports it.
- **Checking them twice could drift:** a second, weaker copy of the verdict, about 60 lines of
  probe script plus a parser.
- **What per-run verification doesn't cover:** the guest's fd table (the guest's own
  `close_range`, covered by `test_what_the_guest_sees`) and supplementary groups (N6's
  documented gap, which nothing can drop).

**The alternative:** §5 as written.

### S6-D2 (fixed; forced by S3-D1; amends contract §4, §7 and §10) — Native adds nothing to `cleanup_job()`

The step-1b amendment in §4, §7 ("generalized … tracking … `cleanup_job()`") and §10 step 6 all
expect a native contribution to shutdown. Step 3's S3-D1 removed its only object, the host temp
dir.
- Native backends aren't tracked for shutdown. `cleanup_job()` and `aclose()` stay
  container-only; native never exists on Windows, where `aclose()` runs.
- The shutdown line still says "nothing to clean up" with a native run in flight, which is true
  of the server's side: the run ends with the server.
- **A test proves it through the real path:** a stdio server with a native run in flight, stdin
  closed the way the SDK closes it → no guest process and no new scope within 2 s (criterion 11,
  end to end; §4.3).

### S6-D3 (fixed; implements N5 and §5) — The backend values and the order

| `VESTIBULE_BACKEND` | On Linux | Elsewhere |
|---|---|---|
| `auto` (default) | N5's order, below | M1's container selection, unchanged |
| `native` | native only: `native` or `native-degraded`, else `Blocked:`. The container is never probed | restart-required: "native isolation needs Linux" |
| `container` | M1's container selection | the same |
| `naive` | unchanged | unchanged |
| anything else | restart-required, naming the four values | the same |

**Config first:** `VESTIBULE_RUNTIME` is checked against its three values before anything is
probed, on every path that can reach the container (`auto`, `container`), so a typo can't hide
behind a working native (skeptic finding 11).

**`auto` on Linux:**
1. The native candidate (S6-D4). If its verdict is `native`, it's chosen; the container is never
   probed.
2. Otherwise, M1's container selection. **When a `native-degraded` candidate is already in
   hand, the S5-D2 soft-off retry is skipped:** its only possible result, `container-degraded`,
   ranks below what we have (finding 7).
3. The pick: `container` > `native-degraded` > `container-degraded`. The loser is drained before
   selection returns (M1's rule for rejected candidates).
4. Neither: `Blocked:` with both reasons, "no isolating backend is available — native: <why>;
   container: <why>". It's restart-required only if both parts are; otherwise S5-D1's 30 s
   re-check applies.

- **A container refusal doesn't block `auto`** when `native-degraded` is there: Docker not
  installed, on a Linux host without systemd, still gets native, degraded and labelled.
- **"Degraded" is one tier, as N5 signed it** (finding 12). That's wrong in one rare case: a
  container missing only `cpu` (S7-D3's partial readback) carries a memory cap, while a
  native-degraded with no user manager carries none, and N5's own reason for ranking the
  container first is the memory cap. With step 2 above, a `container-degraded` can only meet a
  `native-degraded` through that partial readback (cgroup-v1 hosts, Docker Desktop quirks).
  Named here; ranking by "memory applied" is the alternative, if Victor wants it.
- **`Selection.verdict` for native** is the probe run's isolation. Each native run then reports
  its own, so a later run on a busy host can say `native-degraded`. Container runs report the
  probe-time status, as in M1.

### S6-D4 (fixed; implements §5's messages, S2-D4, S2-D9, step 2 §6, step 3 §5) — Native's pre-checks, the probe, and the messages

In order, cheapest first. Each failure is a reason; `auto` logs it as a warning and goes on to
the container, `native` refuses with it.
1. **Pinned images** (S2-D4): each image ref through `registry.parse_ref`. Unpinned: its own
   message, "… is not pinned: native needs a digest reference, like …". Config, so
   restart-required when native is forced.
2. **Rootfs:** both trees (`rootfs.find`). Missing: "no native rootfs in <dir>; run
   `<command>` once (downloads about 123 MB)". `<command>` is the exact line, with every
   non-default `VESTIBULE_ROOTFS_DIR`/`VESTIBULE_IMAGE_*` in front, from a new pure
   `rootfs.setup_command(limits)` (step 2 §6; finding 3). In `auto`'s log line: "…, then restart
   Vestibule to use native isolation".
3. **Overlap:** `rootfs.overlap_problem(limits)`, its text as is (S2-D9). Restart-required when
   forced.
4. **The probe** (S6-D1). A failed probe's reason is:
   - the run's detail, when the sandbox failed (`none`: "setup failed at <stage>: …" or "no
     status report"); or
   - **when the sandbox was fine but the guest failed** (the verdict is native-tier, finding 4):
     M1's form, "probe exit <n>, timed out <yes/no>: <first stderr line>", and M1's own
     "could not use the workspace" refusal text when that line names `/workspace` (a guest
     earlier left it unusable).

   Then a hint, a pure function of the reason and two sysctls:

   | When | Hint |
   |---|---|
   | `kernel.apparmor_restrict_unprivileged_userns` is 1, and setup failed at any stage | "Ubuntu restricts unprivileged user namespaces; lifting that needs root, so use the container backend" |
   | `user.max_user_namespaces` is 0 | "user namespaces are disabled on this host" |
   | the reason names `libseccomp` | "install libseccomp2; native isolation requires it" |
   | setup failed at `mounts` on `/proc` with a permission error (step 3 §5) | "the server runs where /proc isn't fully visible (inside a container, or under systemd's ProtectProc=), so native can't build its sandbox here" |

   The AppArmor row keys on the sysctl, not on a stage (finding 8). The sysctls come from a
   separate reader, so tests inject them (criterion 13).
5. **Unexpected errors** in steps 1–3 (user-owned files and settings) become a native reason,
   never an exception out of selection (finding 10, narrowed: `rootfs.find` already swallows
   `OSError`, per the verifier).
6. **Every native probe logs its outcome and duration** (step 4's carried item: a host where
   placement always waits out its 5 s shows as a ~5 s probe).

### S6-D5 (fixed; contract §4.1, M1 S7-D1) — Native runs share the concurrency budget

`NativeBackend(slots=…)`, the same parameter `ContainerBackend` takes; the selector passes its
`_slots`. Built without it (tests that construct the backend directly), it keeps its own lazy
semaphore. **The test asserts identity:** the selected native warden's semaphore *is*
`selector._slots`, as `test_reselection_shares_one_slot_budget` does for containers (finding 5:
a behavioural "second run refused" test passes either way when `max_concurrent=1`).

### S6-D6 (fixed; implements N5; extends S5-D1) — When a cached selection is re-checked

S5-D1 caches a success for the life of the process. Two native cases would turn that into a
lasting downgrade the user never chose (finding 1):
- **`native-degraded` won because the container was refused *retryably*** (the daemon not up
  yet). That selection is **provisional**: the first call after S5-D1's 30 s cooldown selects
  again. A full container by then wins (N5); otherwise native-degraded is chosen again, with a
  new cooldown. A restart-required container refusal (Docker not installed) makes it final.
  - **Cost:** while it lasts, one call every 30 s pays a container version check, ~0.1 s for a
    daemon that's down, up to its 5 s bound for one that's wedged (M1's exposure for failed
    selections).
- **A `native` selection whose run reports `native-degraded`** (for example the user manager
  went away) is dropped by `note_result`. The next call selects again; it can't loop, because a
  `native-degraded` selection isn't dropped for a degraded run.
- **A native-tier selection whose run reports `none`** is dropped, as now (the rootfs removed
  after selection, criterion 13).

Each drop and each provisional re-check is one log line.

### S6-D7 (fixed; contract §5, step 4 §6) — The degraded detail names why the limits are missing

Contract §5 puts it "in the degraded detail", and step 4 carried "names the reason the log has"
forward (finding 2): the model reads the detail, not the server log.
- **`native_cgroup.place` returns a short reason** instead of `None` when it places nothing: "no
  cgroup v2", "no systemd user bus", "busctl not found", "systemd refused the scope",
  "systemd did not place the run within 5 s", "cgroup layout failed". It still logs the long
  form.
- **`_verdict`** appends it when no limit applied for that reason:
  `native-degraded (limits not applied: cpu, memory, pids — no systemd user bus)`.
- Tests that fake `place` with a `Placement` are unchanged; the step-4 tests that assert the
  exact all-limits detail gain the reason.

### S6-D8 (fixed) — M1's tests pin `backend=container`

They test M1's container selection; on a Linux host with a rootfs, `auto` now means native
first. The list, as the verifier checked it (finding 9):
- `test_select.py`: the `_limits()` helper defaults to `container`;
- `test_container.py`: its four real-selection tests;
- `test_acceptance.py` (the M1 exam);
- `test_stdio_e2e.py`: the `server` fixture's settings, and the SDK-client test's own env dict
  (unpinned, it would wait out its 180 s timeout before failing);
- `stdioutil.py`'s spawn pattern also matches native's "starting a native sandbox" line, for
  §4.3's in-flight test.

`test_lifecycle.py` builds `ContainerBackend` directly and needs nothing. The full Linux suite
then finds any other. Windows: `auto` is the container there anyway.

### S6-D9 (fixed; contract §7) — Config and docs

- No new setting. The comment on `Limits.backend` lists the four values.
- `README.md`: the `VESTIBULE_BACKEND` row (the four values; on Linux, `auto` uses native once
  `setup` has run), and the isolation-values sentence gains `native-degraded`.
- Everything else waits for step 7.

### S6-D10 (Victor, 2026-10-27: (b); S3-D9 vs S5-D8) — What "retire the bypass" means

S3-D9 says step 6 "adds the test proving every selector-built native backend loads the
filter"; step 5 (S5-D8, its §6 and `native.py`'s docstring) says step 6 "retires the bypass".
Two readings (finding 13):
- **(a) Keep the parameter, prove it unreachable.** A test records every `NativeBackend` the
  selector builds and asserts none skips the filter. The docstrings say "unreachable from the
  server". Smallest change. The guard mostly repeats the verdict: a bypassed probe reports
  `none` and is rejected anyway.
- **(b) Delete it.** `_skip_seccomp_for_tests` and the helper's `skip_seccomp` branch go. Its
  nine call sites (eight subprocess runners in `test_native.py`, one offline busy test in
  `test_native_verdict.py`) move to the real filter and assert a native-tier verdict. The
  "filter not loaded → `none`" rule stays covered by the verdict table's `seccomp: False` row.

**Decided: (b), as recommended.** It takes a test-only switch out of the code that builds the sandbox,
and the subprocess tests then run what users run. About nine test edits and five fewer source
lines.

## 3. Changes

| File | Change |
|---|---|
| `src/vestibule/backends/select.py` | the four values, the runtime check first, the dispatch (S6-D3); M1's body becomes `_select_container`; `_try_native`, its pre-checks, the reasons and hints, the sysctl reader (S6-D4); N5's order, the skipped soft-off retry; provisional selections and `note_result`'s two rules (S6-D6) |
| `src/vestibule/backends/native.py` | the `slots` parameter (S6-D5); the placement reason into `_verdict` (S6-D7); the bypass removed (S6-D10) |
| `src/vestibule/backends/native_cgroup.py` | `place` returns its reason (S6-D7) |
| `src/vestibule/backends/native_helper.py` | the `skip_seccomp` branch removed (S6-D10) |
| `src/vestibule/rootfs.py` | `setup_command(limits)` (S6-D4) |
| `src/vestibule/config.py` | the comment on `backend` |
| `tests/test_select.py` | `_limits()` pinned (S6-D8); §4.1 |
| `tests/test_native_select.py` | new: §4.2 |
| `tests/test_container.py`, `tests/test_acceptance.py` | pinned to `container` (S6-D8) |
| `tests/test_stdio_e2e.py`, `tests/stdioutil.py` | pinned; the native spawn pattern; §4.3 |
| `tests/test_native.py`, `tests/test_native_verdict.py` | the step-4 detail assertions gain the reason (S6-D7); the nine bypass call sites move to the real filter (S6-D10) |
| `README.md` | the config row and the isolation values (S6-D9) |
| `docs/plans/M2/M2-native-warden.md` | amendment blockquotes: §4, §5, §7, §10 (S6-D1, S6-D2, S6-D7) |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md` | the sign-off bullet, the step-6 entry |

**Selection, as built:**

```
get(): lock; cache (a provisional selection expires after 30 s); S5-D1 cooldown
_select(limits):
  naive      → as M1
  bad VESTIBULE_RUNTIME (container or auto) → restart-required
  container  → _select_container(degraded_retry=True)                (M1's body)
  native     → not Linux: restart-required; else _try_native() → selection or Blocked
  auto       → not Linux: _select_container(degraded_retry=True)
               Linux: n = _try_native();  n is native → n
                      c = _select_container(degraded_retry = n is None)   (a refusal is kept)
                      c full > n degraded (provisional if c's refusal was retryable)
                        > c degraded > Blocked(both reasons)
  otherwise  → restart-required, naming the four values
_try_native(limits) → (selection | None, reason):
  pinned refs? both trees? no overlap?  (an unexpected error is a reason too)
  run the probe through NativeBackend(slots); passed → Selection(backend, isolation, detail)
  failed → drain the backend; reason = detail, or M1's "probe exit …" form; + the hint
```

**Size check** (step 5's measure):
- **`select.py`:** 328 → about 460: the dispatch and runtime check ~20, `_try_native` ~45, the
  order with provisional selections ~40, the hints and the sysctl reader ~25, `note_result`
  ~5. Look again past ~480.
- **`native.py`:** 439 → about 440 (+6 for slots and the reason, −5 for the bypass).
- **`native_cgroup.py`:** about the same; **`rootfs.py`:** about +8.
- **Tests:** `test_select.py` ~+150, `test_native_select.py` ~180, `test_stdio_e2e.py` ~+60.

**Build order, each step with its check:**
1. The four values, the runtime check, the dispatch, `_select_container` → `test_select.py`
   green on Windows with `_limits()` pinned; the values and runtime-typo tests.
2. `NativeBackend(slots=…)` → the identity test.
3. `setup_command`, `_try_native`, its pre-checks and the probe → on WSL, `auto` selects native
   and a `server.call_tool` run reports `isolation: native`; the unpinned, no-rootfs and
   overlap tests.
4. N5's order and the skipped retry → §4.1's order table; the real "no bus, Docker up →
   container" test.
5. The reasons and hints → the hint table; the guest-failure reason; the injected AppArmor,
   libseccomp and `/proc` tests.
6. The placement reason (S6-D7) → the no-bus detail names it; the step-4 assertions updated.
7. Provisional selections and `note_result` → their rows; the rootfs-removed test.
8. S6-D10 (b) → the nine call sites on the real filter; `test_native.py` passes on 3.14 and
   3.11, and no `_skip_seccomp` / `skip_seccomp` remains under `src/`.
9. The stdio tests: criterion 17 and criterion 11 end to end; the M1 e2e tests pinned.
10. The remaining M1 pins; the full Linux suite green with 0 skips, which finds any unpinned
    test.
11. Mutation checks (§4.4) → each guarding test turns red.
12. Both platform gates green, inside the 540 s budget; the time recorded.
    - **Expected cost:** about +20 s per run that includes these tests (a dozen real
      selections at ~0.1 s, the container-wins and drain tests ~3 s, two stdio launches ~5 s).
      The gate took 207 s after step 5, so about 230 s. Past 250 s, the slow tests get looked
      at.
13. The size check, then docs → `tests/test_docs.py` passes.

## 4. Tests

### 4.1 All platforms, offline (`test_select.py`, with fakes)

- **The values:** unknown → restart-required, naming all four; `native` off Linux →
  restart-required; `auto` off Linux never calls `_try_native`; a bad `VESTIBULE_RUNTIME` is
  refused before any native probe.
- **N5's order,** with `_try_native` and `_select_container` replaced by fakes:

  | native | container | chosen |
  |---|---|---|
  | `native` | (never probed) | native |
  | `native-degraded` | `container` | container; the native candidate drained |
  | `native-degraded` | `container-degraded` | native-degraded; the container candidate drained |
  | `native-degraded` | refused, retryable | native-degraded, provisional |
  | `native-degraded` | refused, restart-required | native-degraded, final |
  | refused | `container` | container |
  | refused | `container-degraded` | container-degraded |
  | refused | refused | `Blocked:` naming both |
  | forced: refused | (never probed) | `Blocked:` with native's reason |
  | forced: `native-degraded` | (never probed) | native-degraded |

  Plus: with a `native-degraded` candidate, `_select_container` is asked without the soft-off
  retry.
- **Restart-required** only when both parts are.
- **Provisional:** before 30 s the cached selection is returned; after it, selection runs again
  and a now-full container wins.
- **`note_result`:** a `native` selection and a `native-degraded` run → dropped; a
  `native-degraded` selection and a degraded run → kept; any native-tier selection and a `none`
  run → dropped.
- **The reasons and hints:** each hint row, none; the guest-failure reason in M1's form; the
  workspace case uses M1's text.
- **`setup_command`:** defaults → the bare command; a non-default `VESTIBULE_ROOTFS_DIR` and an
  image override → both in front, shell-quoted.
- **The server** renders a native refusal as `Blocked:` (as M1's test does).

### 4.2 Linux, `native` marker (`test_native_select.py`, real sandboxes)

- **`auto` selects native** (`@cgroups_required`), and a `server.call_tool` run says
  `isolation: native`.
- **The shared budget:** the selected warden's semaphore is `selector._slots`.
- **No bus** (`bus_env` → `None`): forced `native` → `native-degraded`, and the detail ends
  "— no systemd user bus".
- **No bus, Docker up** (`docker` marker too): `auto` → `container`.
- **The drain** (finding 6): forced `native` with the probe script patched to sleep and the
  probe timeout short → `Blocked:`; right after `get()` returns, no native finisher task is
  pending and no helper process is left. Without the drain, the helper is still being stopped.
- **Unpinned image** (`image_python` without a digest): forced `native` → the "digest
  reference" message.
- **No rootfs** (an empty `rootfs_dir`): forced `native` → `Blocked:` whose command starts with
  that `VESTIBULE_ROOTFS_DIR=…`; `auto` with Docker up → `container`.
- **Overlap** (the workspace inside the rootfs directory): forced `native` → the overlap text.
- **The guest fails, the sandbox is fine** (the workspace made unusable: `chmod 000` on it) →
  the reason is M1's workspace refusal text, not an empty one.
- **Rootfs removed after selection:** the next run reports `none`; the call after it selects
  again (forced `native` → the `setup` message).
- **User namespaces refused** (criterion 13, injected): the sysctl reader says
  `apparmor_restrict_unprivileged_userns = 1`, and a stand-in helper fails at the `helper`
  stage with EPERM on `setgroups` (and, separately, at `mounts`) → both name Ubuntu's
  restriction.
- **No libseccomp** (injected): a stand-in helper sends step 5's import error → "install
  libseccomp2".
- **`/proc` not fully visible** (injected): a stand-in helper fails at `mounts` with EPERM on
  `/proc` → the `/proc` hint.
- **S6-D10 (b):** the moved subprocess tests pass on the real filter.

### 4.3 Stdio end to end (Linux, `native` marker)

- **Criterion 17:** the server launched with only `HOME LOGNAME PATH SHELL TERM USER` (plus
  `VESTIBULE_WORKSPACE`) → `run_code` → `isolation: native`; it found the user bus itself.
- **Criterion 11, end to end** (S6-D2): a native run in flight (seen through the native spawn
  line), stdin closed the way the SDK closes it → no process holds the run's token and no new
  `vestibule-*.scope` exists within 2 s.

### 4.4 Mutation checks (during the build; not committed)

Each must turn its guarding test red:
- `auto` on Linux skips native (M1's behaviour) → "`auto` selects native";
- the container preferred over full native → order row 1;
- native-degraded preferred over a full container → order row 2 and the real Docker test;
- the soft-off retry not skipped → the "without the retry" row;
- a retryable refusal treated as final → the provisional rows;
- `note_result` keeps a `native` selection after a degraded run → its row;
- the loser not drained → the drain test;
- no pin, rootfs or overlap check → their tests;
- `setup_command` drops the env prefix → its rows and the no-rootfs test;
- the guest-failure reason left empty → its row and the `chmod 000` test;
- the AppArmor hint keyed on the `mounts` stage → the `helper`-stage injection;
- no placement reason → the no-bus detail;
- the native backend built without the shared `slots` → the identity test;
- the runtime check after the native probe → the runtime-typo test.

### 4.5 Done when

- **Gates:** both platform gates green; on Linux, 0 skips, inside 540 s.
- **Size:** §3's check, any overrun recorded.
- **Review:** `critic` and `verifier` leave no open CONFIRMED high or medium finding.
- **Docs:** the README rows, the contract amendments, a HISTORY bullet, a BUILD_LOG entry.

## 5. Edge cases

- **How long selection can take** (the verifier's sum, finding 7), all before the run's outer
  deadline starts, with the selection lock held:
  - one native probe: slot wait 5 s + placement 5 s + its 10 s timeout + 2 s grace + 5 s status
    wait, so at most **27 s** to its result, and **37 s** with the drain of a failed one;
  - `auto` on a host where native is degraded and the container daemon is wedged: that, plus
    M1's own container bound (unchanged), less the skipped soft-off retry;
  - normally ~0.1 s (native full) or ~2 s (native degraded, Docker up).

  Concurrent first calls wait on the lock, as in M1.
- **`setup` after the server chose the container:** the container stays until a restart (a
  cached success). The log line says so.
- **A concurrency refusal during a re-selection** (every shared slot held by a dropped backend's
  runs): the probe's "too many concurrent runs" refusal propagates and is cached like any
  failure, as in M1 today. Unchanged here.
- **A corrupted node tree with its marker intact:** the probe only runs bash, so each node run
  reports `none`, drops the cache, and the next call probes again (~0.1 s each time). The cache
  belongs to the user (contract §6's trust statement); the run says what failed.
- **A request cancelled during the native probe:** the run's own finisher stops the helper, as
  for any run (step 3); PDEATHSIG ends it if the server exits first.
- **A forced `native` with Docker installed:** Docker is never asked.
- **Windows and macOS:** unchanged. `select.py` imports `native.py` on every platform, which it
  already supports (step 3's platform guards); native is never probed there.
- **The stalled-stderr known issue** (BUILD_LOG): selection logs a few lines per selection.

## 6. Carried forward

- **Step 7:** the README Linux section and per-distro table (Ubuntu's restriction, what `auto`
  picks where), the benchmark, the doc wording, the live-agent check, and criterion 9's two
  remaining parts.
- **M4:** the AppArmor message on a stock Ubuntu 24.04+ VM (contract N5).

## 7. Review record

`skeptic` reviewed draft v1 on 2026-10-27. Verdict: **"sound with amendments"**, and no finding
on S6-D1. The `verifier` then checked the review's fifteen claims about the code: twelve
confirmed, one plausible (C14, AppArmor's failing stage: not reproducible here), one refuted
(C10: `rootfs.find` doesn't raise for an unreadable cache, though bare `Path.is_dir()` does on
3.11), and one sum (C15: 27 s, 37 s with the drain, not the 15 s v1 said).

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | med | A `native-degraded` win is cached for the process even when the container was only refused retryably, or when a `native` selection later degrades: defeats S5-D1 and N5's reason for ranking the container higher | Accepted: provisional selection re-checked after the cooldown; `note_result` drops a `native` selection after a degraded run (S6-D6) |
| 2 | med | The cause of degradation reaches only the log; contract §5 and step 4 §6 put it in the detail | Accepted: `place` returns its reason; `_verdict` appends it (S6-D7) |
| 3 | med | Three carried message requirements dropped: the exact `setup` command (step 2 §6), "native needs a digest reference" (S2-D4), the `/proc` message (step 3 §6). Verifier: C1–C3 confirmed | Accepted: S6-D4 steps 1–2 and the hint table |
| 4 | med | The reason is empty when the sandbox is fine but the guest fails. Verifier: C5 confirmed | Accepted: M1's "probe exit …" form and workspace text (S6-D4 step 4) |
| 5 | med | The shared-budget test can't fail (the backend's own semaphore is also 1). Verifier: C4 confirmed | Accepted: an identity assertion, as M1's (S6-D5) |
| 6 | med | The drain test can't fail: a degraded probe's helper is gone before the container probe returns | Accepted: drain guarded by a timed-out probe; no pending finisher right after `get()` (§4.2) |
| 7 | low | The probe's time bound understated; `auto`'s chain never added up; the soft-off retry is wasted next to a native-degraded candidate. Verifier: C15 | Accepted: the sums in §5; the retry skipped (S6-D3) |
| 8 | low | The AppArmor injection fails at `mounts`; stock Ubuntu fails at the helper's id-map write. Verifier: C14 plausible | Accepted: keyed on the sysctl at any stage; injected at both stages; M4 confirms (S6-D4) |
| 9 | low | The test-migration list was wrong: `test_lifecycle` has no selector tests; `test_container.py`'s four were missing; the stdio spawn regex and the SDK test's own env. Verifier: C6–C9 confirmed | Accepted: S6-D8 corrected |
| 10 | low | Non-`RunRefusedError` errors in the native path would escape selection. Verifier: C10 refuted the main example | Accepted, narrowed: errors in the pre-checks become a reason; the busy refusal stays as in M1 (S6-D4 step 5, §5) |
| 11 | low | A `VESTIBULE_RUNTIME` typo is hidden while native is full. Verifier: C13 confirmed | Accepted: checked first (S6-D3) |
| 12 | low | "Degraded" treated as one tier: a native-degraded without a memory cap beats a container-degraded with one | Accepted as stated: N5's signed order kept, the case named, the alternative offered (S6-D3) |
| 13 | low | "Retire the bypass" read one way silently; the guard repeats the verdict. Verifier: C11 confirmed | Accepted: both readings to Victor, (b) recommended; the grep test dropped (S6-D10) |
| 14 | low | §7 and §10 of the contract still expect native in `cleanup_job()`; the shutdown line. Verifier: C12 confirmed | Accepted: §4, §7, §10 amended; the line explained (S6-D2) |

### `critic` on the diff (2026-10-27), checked by `verifier`

**Round 1:** eight findings, all CONFIRMED. Each code fix came with a test that failed on the
code before it (six did), or, for a test gap, a mutant that survived before it.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| 1 | med | The provisional re-check reran the whole selection, native probe included. That probe waits for a slot its own runs hold; with `max_concurrent=1` and a run in flight it failed, and the combined refusal blocked every call for 30 s while native-degraded worked | Fixed: `_recheck_container` asks only the container, postpones while every slot is busy, and native keeps serving whatever the container says |
| 2 | low-med | Any container refusal made native-degraded provisional, including the deterministic "full profile failed its probe": a re-check every 30 s for the life of the process | Fixed in round 2 (below) |
| 3 | low | A `busctl` that can't connect (an inherited `XDG_RUNTIME_DIR` with no bus behind it) was reported as "systemd refused the scope" | Fixed: "Failed to connect" → "no systemd user bus" |
| 4 | low | `setup_command` left out `VESTIBULE_WORKSPACE` (setup's overlap check) and every default, so a value exported in the user's shell would win | Fixed: all four settings setup reads, paths expanded and quoted |
| 5 | low | With Ubuntu's sysctl at 1 (its default), every native failure got the AppArmor hint, a missing libseccomp included | Fixed: evidence in the reason first (libseccomp, `/proc`); the sysctl hints only for an actual setup failure |
| 6 | low | No test mixed a restart-required and a retryable refusal: `and` → `or` survived | Fixed: the mixed test |
| 7 | low | The stalled-stderr runner only reported "ended", so a run that failed at setup passed for the self-deadline | Fixed: it reports and asserts a native-tier verdict and exit −1 |
| 8 | low | Size: `_check_runtime` had one caller; both probes built the same script | Fixed: inlined; `_probe_script` shared |

**Round 2** (the fixes touched more than the findings' lines): one medium and three lows, all
CONFIRMED.

| # | Sev. | Finding | Disposition |
|---|---|---|---|
| G1 | med | Round 1's F2 fix made only a down daemon provisional. The image preflight's "runtime did not respond" and "image not usable, `docker pull` it" are plain refusals too, so a half-booted Docker or unpulled images made native-degraded final until a restart | Fixed by inverting the rule: final only for `_RestartRequired` and the new `_ProbeFailed` (the full profile ran and failed its probe), both raised in `select.py`; any other refusal keeps the selection provisional. `_NotYet` went |
| G2 | low | The "Docker came up" test passed even if the switch never stuck | Fixed: the cached selection is the container, and it isn't asked again |
| G3 | low | The `/proc` hint matched "/proc" anywhere, so a rootfs path like `~/procurement/rootfs` turned any mount EPERM into it | Fixed: only a mount *of* `/proc` ("/proc: ") |
| G4 | low | The drain around the native probe's `run` waits on nothing in the busy case | Kept: an exception after the run's finisher exists (a read error in collection) leaves a finisher to wait for. The verifier confirmed both halves |

The round-2 fixes stayed inside their findings' lines (one class for another, one
classification test, one `except` order, one substring), so no third round ran; the five
mutants below each turned their test red.

## 8. As built (2026-10-27)

- **Mechanism, on WSL:** `auto` selects native, and a `server.call_tool` run says
  `isolation: native`; the server launched with only the MCP SDK's six variables finds the bus
  itself (criterion 17); a server closed mid-run leaves no guest and no scope within 2 s
  (criterion 11, end to end).
- **Sizes** (code lines, step 5's measure):
  - `select.py`: **493** (estimate ~460, look again past ~480). Looked at: about 22 lines are
    `_recheck_container`, the fix for round 1's medium finding, and round 1's simplifications
    took back about 5. Nothing else could go without losing a stated behaviour.
  - `native.py`: 445 (~440). `native_helper.py`: 379 (329 + step 5, less the bypass).
    `native_cgroup.py`: 137. `rootfs.py`: +9.
- **Mutation checks:** 17 from §4.4, 7 from round 1 and 5 from round 2, all red. One mutant
  survived on the first pass: the overlap test asserted `"overlap" in msg`, and pytest's
  `tmp_path` holds the test's own name, so the "no native rootfs" refusal that follows matched
  too. The assertion now uses the overlap message's own words.
- **Gate time:** 226 s, 256 s, then 248 s (final), on Linux. The `--durations` run put none
  of step 6's tests in the slowest twelve; those are M1's Docker acceptance tests and step 2's
  Docker Hub fetches, whose latency varies from run to run.
- **Departures from the plan's wording:**
  - **Pre-check order** is pins → overlap → rootfs, not pins → rootfs → overlap: `setup`
    itself refuses an overlap, so sending the user to `setup` first would loop.
  - **The no-rootfs message** carries "restart Vestibule if it already chose another backend"
    itself, not only in `auto`'s log line.
  - **S6-D6 as built** (round 1's F1, round 2's G1):
    - the re-check asks only the container, and postpones while every shared slot is busy;
    - a selection is final only on a restart-required refusal or the full profile failing its
      probe; any other refusal keeps it provisional;
    - the cost while provisional is one container check per 30 s: a version check (~0.1 s)
      while the daemon is down, plus an `image inspect` once it's up.
  - **`setup_command`** names all four settings setup reads, defaults included (round 1, F4),
    not only the non-default ones.
  - **The step-5 default-vs-bypass test** went with the bypass: the hello test already checks a
    native-tier verdict, and the verdict table's `seccomp: False` row keeps the rule.
  - **No test renders a native refusal through the server:** `test_server_renders_selection_
    refusal_as_blocked` covers the path for every backend.
