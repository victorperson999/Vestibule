# Vestibule — Build History

A running, plain-language log of what has been built and decided, milestone by milestone.
New entries are appended under their milestone as work lands: one point per build step,
one point per decision sign-off. (Detailed contracts live in `docs/plans/`; the roadmap
is `docs/PLAN.md`.)

---

## M0 — Skeleton that runs, unsafely (✅ accepted 2026-07-02)

Goal: prove the end-to-end plumbing with zero isolation.

- Built the MCP server (`src/vestibule/server.py`) over stdio with one tool,
  `run_code(language, code, timeout_seconds)`. It validates and clamps every argument in
  the clean server process, truncates guest output to protect the model's context, logs
  to stderr only (stdout is the JSON-RPC channel), and never lets a handler exception
  kill the session.
- Built the `Warden` abstraction (`backends/base.py`): every backend returns a
  `RunResult` that honestly reports its `isolation` level.
- Built the `NaiveBackend` (`backends/naive.py`): runs code via plain subprocess with a
  wall-clock timeout — **no isolation**, reported honestly as `isolation: none`. Guest
  stdin is always DEVNULL: inheriting the server's stdin would leak the MCP channel to
  untrusted code (and hangs the child on Windows). That rule now applies to all backends.
- `config.py`: frozen limits dataclass with `VESTIBULE_*` env overrides; allowed
  languages python / bash / node.
- **Accepted:** a live agent session called `run_code` with `print("hi from vestibule")`
  over real MCP stdio and got `exit_code: 0`. Four smoke tests, ruff + mypy clean.
  Registered with Claude Code.
- Post-M0: Codex adversarial review of the M1 design — 20 ranked findings
  (`docs/reviews/M1-codex-adversarial-review.md`), all folded into the M1 contract
  (`docs/plans/M1-container-backend.md`: decisions D1–D4, 7 build steps, 14 acceptance
  criteria).

---

## M1 — Container backend + workspace (🔨 in progress)

Goal: real isolation via a throwaway Docker container per run — the cross-platform
floor — plus the persistent workspace and the `read_workspace` tool.

### Decision sign-offs

- **D1 — approved 2026-07-03.** The workspace is one dedicated writable folder
  (`~/.vestibule/workspace`), mounted at `/workspace` in every container. Sandboxed code
  may create, modify, or delete files there — a declared, contained blast radius inside a
  Vestibule-owned folder, never user data. Overridable: `VESTIBULE_WORKSPACE` (location),
  `VESTIBULE_WORKSPACE_RO=1` (read-only mode).
- **D2 — approved 2026-07-03.** Two official images — `python:3.12-slim` (also runs
  bash) and `node:22-slim` — pinned by digest so the image bytes can never silently
  change. Vestibule **never auto-pulls**: a missing image returns a Blocked message with
  the exact `docker pull` command; pulling is a one-time, documented, user-initiated
  setup step.
- **D3 — approved 2026-07-03.** Tiered degradation. *Hard* controls change what hostile
  code can reach — no network, non-root user, all capabilities dropped,
  no-new-privileges, read-only rootfs, working workspace mount — and if any is
  unenforceable the run is refused (`Blocked:`). *Soft* limits only bound consumption —
  memory/CPU/pids/tmpfs caps, all backstopped by the external timeout — so a missing one
  degrades loudly instead: the run proceeds but reports
  `isolation: container-degraded (limits not applied: …)`. Plain `container` is only
  reported when the full profile verifiably applied; `isolation: none` is unreachable
  unless the user explicitly sets `VESTIBULE_BACKEND=naive`.
- **D4 — approved 2026-07-03.** Docker-first but runtime-agnostic. The backend never
  assumes a runtime by name — it verifies capabilities via the D3 probes, so any runtime
  that passes is safe to use. `VESTIBULE_RUNTIME=auto|docker|podman` exists from day one
  (auto prefers Docker); only Docker is tested and supported in M1, and Podman is
  documented as experimental until a dedicated validation pass.

- **S4-D1 — approved 2026-07-04.** A 5th concurrent `run_code` call waits up to 5 s for a
  slot, then gets a legible `Blocked: too many concurrent runs (max N); retry shortly`
  instead of queueing into the outer deadline's generic failure. Carried by a new typed
  `RunRefusedError` (backends → server), which step 5 will reuse for probe failures.
- **S4-D2 — approved 2026-07-04.** Missing-runtime honesty fix: a run that never spawned
  now reports `isolation: none` ("runtime unavailable; nothing was executed"), never
  `container`.
- **S4-D3 — approved 2026-07-04.** Deadline-label reaping, replacing the planned age
  heuristic after a Codex adversarial review of the step-4 plan (finding: a reaper using
  *local* timeout config can kill live runs of another Vestibule server configured with a
  longer timeout). Every container is stamped at spawn by its owner with
  `vestibule.deadline=<unix epoch>` (spawn + its own timeout + 90 s); any reaper removes a
  labeled container — in any state — only when now > deadline + 60 s, skipping its own
  in-flight runs. Also fixes two v1-plan bugs the review surfaced: the created-state race
  (reaping a container in the create→start window) and Docker Desktop VM clock drift
  (no daemon timestamps are consulted at all). Missing/garbled deadline ⇒ skipped loudly,
  never removed — note: containers from before step 4 carry no deadline label, so any dev
  leftovers need a one-time manual `docker rm`.

### Build steps (7 planned)

- **Step 1 — done 2026-07-03.** Config + path jail. `config.py` gained the M1 settings
  (workspace location/read-only, runtime & backend selection, image references,
  max concurrency, tmpfs size — all env-overridable). New `src/vestibule/workspace.py`:
  the `read_workspace` path jail — rejects traversal (`..`), absolute paths, drive
  letters and NTFS streams (any `:`), UNC/device prefixes, NUL bytes, Windows reserved
  device names, and trailing dots/spaces, all before touching the filesystem; then walks
  the path refusing symlinks/reparse points anywhere in the chain. 24-case unit suite
  including live symlink attacks (`tests/test_workspace.py`).
- **Step 2 — done 2026-07-03.** Server hardening + `read_workspace` wiring. Every
  `run_code` argument is strictly type-checked before anything is spawned (malformed
  input ⇒ a legible `Blocked:` message, never a crash); the `read_workspace` tool is
  exposed (jail-backed, filesystem work runs off the event loop); `RunResult` gained
  `isolation_detail` and the `container-degraded` vocabulary; the server's outer
  deadline moved to `timeout + 20s` to leave room for container kill/cleanup; the
  `run_code` tool description now honestly tells the model the workspace is writable.
  Validation suite in `tests/test_validation.py`. 49 tests pass; ruff + mypy clean.
- **Step 3 — done 2026-07-03.** `ContainerBackend` happy path
  (`src/vestibule/backends/container.py`). Every run gets a fresh, named, labeled
  container under the full locked-down profile: no network, all capabilities dropped,
  no-new-privileges, read-only rootfs, non-root user, memory/CPU/pids caps, size-capped
  tmpfs `/tmp`, no host environment inheritance. Code is delivered via a per-run host
  temp dir mounted read-only at `/sandbox` (never argv, never stdin); output is
  collected streaming with a hard cap (2× the display limit) — a flooding guest gets
  its container killed early; timeouts kill the container via the runtime, never just
  the CLI process. Both sandbox images pulled on the dev machine. 9 Docker-marked tests
  ran against the real daemon (3-language hello, workspace persistence, network gone,
  rootfs read-only, timeout leaves no survivor, flood capped) — 58 total tests pass,
  ruff + mypy clean. Deferred to step 4 by design: cancellation-shielded cleanup,
  orphan reaping, concurrency semaphore.
- **Step 4 — done 2026-07-04.** Run lifecycle hardening (plan:
  `docs/plans/M1-step4-lifecycle.md`, adversarially reviewed by Codex before
  implementation). Cleanup (container kill/rm + temp dir) now runs as an *independent*
  shielded task, so a cancelled request — outer deadline or MCP client cancel — can never
  orphan a container or leak a temp dir. Orphan reaping per S4-D3: a detached,
  deduplicated pass on first use and after each run (two bounded CLI calls + one batched
  `rm -f`), with the keep/remove rule a pure unit-tested function. Concurrency capped by a
  lazy semaphore (`max_concurrent`, default 4) with the S4-D1 bounded-wait refusal; S4-D2
  honesty fix landed. 17 new tests (12 Docker-free: semaphore cap, busy refusal + server
  rendering, 7-case reap-decision table, cancellation hygiene, label emission; 5
  Docker-marked: cancel mid-run leaves nothing behind, epoch-1970 deadline reaped while
  *running*, future deadline spared, unlabeled spared + warned, 4 simultaneous runs) —
  75 total pass, ruff + mypy clean. Post-implementation Codex review (P2): the shield-awaited
  cleanup could push a timed-out run past the server's outer deadline on a slow daemon,
  swallowing the honest result — fixed by fully detaching cleanup (result returns
  immediately; the finisher task holds the concurrency slot until the container is really
  gone). +1 regression test ⇒ 76 total.
- **Step 4 follow-up — fixed 2026-07-05.** The Codex adversarial review of the step-5
  *plan* re-audited the step-4 code and found the guest-timeout path could still consume
  the entire `timeout_s + 20` outer deadline on a wedged daemon (kill 5 s + rm 5 s + CLI
  wait 5 s serialized after `timeout_s + 5` of collection) — the same failure mode as P2,
  one path over: the honest timed-out result would be replaced by the generic outer-deadline
  message. Fixed by detaching the timeout kill entirely: the timed-out result returns at
  `timeout_s + 5` (now always `exit_code: -1`), the finisher kills the container, and a
  detached reaper collects the CLI process. Bonus honesty fix the same lines hid: a CLI that
  wedges *after* the guest finished is now reported as a runtime failure ("exit code
  unknown"), not a fake guest timeout. +2 regression tests ⇒ 78 total, ruff + mypy clean.
- **S5-D1 / S5-D2 / S5-D3 — approved 2026-07-05** (all as recommended, after a Codex
  adversarial review of the plan and a final logic check). S5-D1: a failed backend selection
  is cached only 30 s, then re-checked — "start Docker Desktop, then retry" works without
  restarting the MCP session; success caches for the process lifetime. S5-D2: when the full
  profile fails but hard isolation works, one retry runs with all four soft limits (memory,
  cpu, pids, tmpfs-size) off, and such runs honestly report `container-degraded` + the exact
  list; per-limit add-back rejected (unbounded first-call latency) unless real degraded
  environments show up. S5-D3 (revised by the Codex review): per-run image preflight +
  `--pull never` land in step 5 itself — `docker run`'s default auto-pull would otherwise
  let a first node run start a multi-minute network pull inside a tool call (D2 violation);
  selection still needs only the python image. Docker CLI ≥ 20.10 becomes the documented
  floor.
- **Step 5 — done 2026-07-05.** Backend selection & capability probing built
  (`src/vestibule/backends/select.py` + wiring). On the first tool call the selector finds a
  runtime (`auto` prefers Docker, D4), then test-drives the FULL locked-down profile as a
  real run through the normal run path — a bash workspace round-trip in `python:3.12-slim`
  (read-only mode instead demands that reads work and writes *fail*) — and commits:
  `container`, `container-degraded` (soft-off retry passed), or every `run_code` Blocked
  with the exact fix. Naive is reachable only via explicit `VESTIBULE_BACKEND=naive`;
  unknown backend/runtime values are legible refusals. The final pre-build logic check
  caught a plan gap: "daemon dies after a good probe" was NOT covered by S4-D2 (that only
  fires when the docker *binary* is gone) — a dead daemon makes `docker run` exit 125 with
  the container never started, which would have reported `isolation: container` for a
  non-run. As built, exit 125 reports `isolation: none` ("nothing was executed") and the
  server's `note_result` hook drops the cached selection so the next call re-probes. Probe
  stderr is scanned for runtime WARNING lines (silently-dropped limits) and logged loudly.
  Server outer deadline `timeout+20` → `timeout+30` to fund the preflight's bounded worst
  case. Also tidied: the overflow-killer's container kill is now finisher-tracked/shielded
  so a run ending mid-kill can't strand the kill's CLI subprocess.
  **Two real bugs found by the build itself:** (1) *CRLF script corruption* — step 3 wrote
  guest scripts in text mode, which on Windows hosts turns `\n` into `\r\n`; CRLF breaks
  bash keywords in the Linux guest (`then\r` ≠ `then`). Python/Node tolerate it and
  single-line bash never hit it — the read-only-mode probe was the first multi-line
  `if/then` bash guest and failed instantly. Fixed with `newline="\n"` on the script write.
  (2) *Failed-selection task leak* — a selection that rejects its probe candidates left
  their detached cleanup/reaper tasks running; event-loop shutdown then mass-cancels them,
  and a task caught mid-`create_subprocess_exec` can orphan the spawn waiter on Windows
  (CPython proactor wart) — deadlocking loop close (the test suite hung in teardown for
  42 minutes; diagnosed with py-spy stack dumps + a watchdog that dumps still-alive tasks
  10 s after cancellation). Fixed: new bounded `ContainerBackend.drain()`, called on every
  rejected candidate; permanent regression test asserts a failed selection leaves zero
  pending tasks. +25 tests (15 Docker-free selection suite, 6 Docker-free
  preflight/honesty, 4 Docker-marked: real selection verdict, probe leaves no trace,
  RO-workspace selection, failed-selection drain) ⇒ 103 total, ruff + mypy clean.
- **Step 6 — pending.** Digest pinning captured from a real `docker pull`, setup-UX
  message polish (per-run image preflight + `--pull never` landed in step 5 via the
  revised S5-D3).
- **Step 7 — pending.** Docker-marked acceptance suite (14 criteria) + README/docs
  update.
