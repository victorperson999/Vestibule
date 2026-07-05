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
- **Step 5 — pending.** Capability probing and honest backend selection
  (`container` / `container-degraded` / blocked with an actionable message).
- **Step 6 — pending.** Digest pinning captured from a real `docker pull`, image
  preflight, setup-UX messages.
- **Step 7 — pending.** Docker-marked acceptance suite (14 criteria) + README/docs
  update.
