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
  (`docs/plans/M1/M1-container-backend.md`: decisions D1–D4, 7 build steps, 14 acceptance
  criteria).

---

## M1 — Container backend + workspace (✅ accepted 2026-10-14)

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
  `docs/plans/M1/M1-step4-lifecycle.md`, adversarially reviewed by Codex before
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
- **Step 6 planned — 2026-07-06** (`docs/plans/M1/M1-step6-pinning.md`, two Codex
  adversarial passes). Four decisions: S6-D1 (pin the multi-arch manifest-list digest,
  proven through the real preflight + `--pull never` path), S6-D2 (un-pinned image
  override ⇒ loud warning, never refusal), S6-D3 (no standalone doctor CLI in M1),
  S6-D4 (below).
- **S6-D4 — approved 2026-07-06.** The pin-proof gate runs under *both* Docker Desktop
  image stores (classic and containerd — they are separate stores and treat
  manifest lists differently), so neither mainstream configuration is claimed but
  untested. If either mode fails, narrowing support comes back as a fresh decision;
  nothing ships on hope.
- **Out-of-band fix — 2026-07-06.** Codex's second step-6 pass also caught a stray
  working-tree edit in `naive.py` that made the spawn-failure path report
  `isolation="false"` (not a valid isolation value). Reverted before commit; permanent
  regression test `test_spawn_failure_reports_isolation_none` in `tests/test_smoke.py`
  ⇒ 104 tests.
- **Workflow change — 2026-10-10.** The build loop moved to the Claude-only process in
  `.claude/WORKFLOW.md` (plan → `skeptic` → sign-off → build → `gate` → `critic` →
  `verifier` → docs). Codex is no longer part of this project's loop.
- **Step-6 plan v2 — skeptic-reviewed 2026-10-10.** First review under the new loop:
  5 medium + 5 low findings, all ten code/Docker claims confirmed by `verifier`.
  Biggest catch: Docker does not check a tag that sits next to a digest
  (`python:3.11-slim@<3.12 digest>` resolves to the 3.12 image), so the planned
  `python:3.12-slim@sha256:…` form carried a label nothing verified.
- **S6-D1 / S6-D2 / S6-D3 — approved 2026-10-10** (all as recommended, v2 plan).
  S6-D1: pin each image's multi-platform index digest in **tagless, fully qualified**
  form (`docker.io/library/python@sha256:…`), captured from a fresh pull and accepted only
  when two independent readings agree and the index lists amd64 + arm64; the tag lives in
  a dated comment, and a Docker test checks the interpreter version inside each pinned
  image. S6-D4's procedure tightened with it: in *each* store mode the store starts empty,
  the shipped ref is pulled exactly as the README says, then the real Docker test suite
  runs. S6-D2: an unpinned image override gets one stderr warning per selection (both
  refs, skipped for naive), never a refusal and never in run results (provenance is M3's
  audit log, not the isolation field); the override-image requirements go in the README.
  S6-D3: no standalone doctor CLI in M1 (M4 candidate). Also adopted from the review:
  setup refusals say *retry* or *restart* according to what actually works (config and
  fresh installs need a restart), cooldown-cached refusals say when they re-check, and
  the README pull commands are test-pinned to the config.
- **Step 6 — done 2026-10-10.** The sandbox images are now pinned to exact bytes:
  `docker.io/library/python@sha256:a6e34c59…` (python:3.12-slim → Python 3.12.15) and
  `docker.io/library/node@sha256:c3de60bf…` (node:22-slim → Node v22.23.3), captured from
  a fresh pull here (both tags had moved since July), cross-checked two ways, confirmed
  multi-platform (amd64 + arm64). Proven the way a new user would hit it — empty store,
  pull by the exact README command, then the real Docker test suite — under **both**
  Docker Desktop image stores (containerd, then classic, then back). Setup UX: README
  gained a one-time setup block (install, `--scope user` registration, the two pull
  commands, honest notes) and its stale Status lines were fixed; every setup refusal now
  says whether to *retry* or *restart*, cached refusals say when they re-check (unless
  only a restart can help), start hints name the right product per runtime, and the
  exit-125 detail no longer tells the model to re-run code that may already have run.
  Tests: the README pull lines and config pins are test-locked together, a Docker test
  checks the interpreter versions inside the pinned images, and Docker tests now *skip*
  with the exact pull commands on a machine without the images (seen working on the
  empty classic store) instead of failing — and can never pull from the network.
  Review: two `critic` rounds (1 medium + 2 low, then 1 medium + 3 low), every finding
  `verifier`-confirmed and fixed. 104 → 122 tests, ruff + mypy clean. Backtracking
  index started: `docs/BUILD_LOG.md`.
- **Step 7 — pending.** Docker-marked acceptance suite (14 criteria) + README/docs
  update. Carries one known issue surfaced in step 6's review (pre-existing since
  steps 4–5): a backend dropped by the server's honesty hook — and the live one at
  shutdown — is never drained, so its cleanup tasks can be cut off at exit (the Windows
  hang `drain()` was built to prevent) and briefly escape the concurrency count. Needs
  its own design + `skeptic` pass in the step-7 plan.
- **Step 7 planned — 2026-10-11** (`docs/plans/M1/M1-step7-acceptance.md`; the M1 plans
  now live in `docs/plans/M1/`). Planning found two new problems, both checked on this
  machine:
  - *Guest-made symlinks are real host symlinks.* Ordinary host tools follow them out of
    the workspace. Vestibule's `read_workspace` refuses them.
  - *Silently dropped limits.* A runtime that drops a resource limit with only a warning
    still got `isolation: container`.

  `skeptic` judged v1's shutdown fix wrong: it would have cancelled the cleanup it waited
  for, and it assumed a client that waits 20 s, when real clients kill the server after
  about 2 s. `verifier` confirmed all 11 code claims, including four live checks:
  - a killed server leaves its container running
  - modules saved to `/workspace` can't be imported
  - one guest can permanently break setup by creating a folder named `.vestibule-probe`
  - a cancelled drain kills its own cleanup
- **S7-D1…D5 — approved 2026-10-11** (all as recommended):
  - **S7-D1:** on shutdown, remove our containers first in one quick batch, never let a
    wait cancel cleanup, use one concurrency budget per process, and hard-exit rather than
    risk the Windows hang.
  - **S7-D5:** every guest runs under `timeout`, so a container ends itself at its labeled
    deadline even if the server was killed. This amends contract §3, and custom images now
    need coreutils `timeout`.
  - **S7-D2:** warn the model (tool description) and the user (README) about guest
    symlinks, and amend D1's wording. A structural seccomp fix is noted for M2, and per-run
    link reporting for M3.
  - **S7-D3:** the probe reads its own cgroup limits back and reports `container-degraded`
    on any mismatch, with a broadened warning scan as the cgroup-v1 fallback.
  - **S7-D4:** archive the M0 walkthrough and write a current dev guide.

  Built in two parts, each reviewed and committed separately: 7a (hardening) and 7b
  (acceptance suite + docs). M1 is called *accepted* only after a live check from a
  restarted Claude Code session.
- **Step 7a — done 2026-10-11** (hardening; 7b, the acceptance suite + docs, is next).
  - **The shutdown hang is real, and fixed.** A new test drives the real `vestibule-mcp`
    over stdio the way an MCP client does. On the old code, closing stdin with a run in
    flight hung the server indefinitely. A stack dump showed `asyncio.run`'s cancel-all,
    the same mechanism as the 42-minute step-5 hang, and the container was still running.
  - **How shutdown works now:**
    - the server removes every container it owns in one batch
    - it waits briefly without cancelling cleanup
    - it hard-exits if anything is still pending
  - **Measured:** shutdown takes 0.2–0.4 s, inside the 2 s that clients allow before
    killing the server, and the tests now kill at 2 s exactly like a client.
  - **Containers end themselves.** Every guest runs under `timeout`, so even a killed
    server can't leave a runaway container. This was proven by running the profile with no
    server attached: a 300 s guest was killed at its deadline and auto-removed.
  - **Limits are verified, not assumed.** The probe reads its own cgroup limits back. A
    limit that isn't really applied, or a container that can't see its cgroup, now reports
    `container-degraded`. This machine verifies all five.
  - **Two lockouts closed:**
    - the probe's workspace file name is now random, so a guest can't block all future
      setups by planting a folder
    - a workspace whose permissions a run changed gets its own clear refusal that names
      the folder
  - **`run_code` description** now explains how to import your own workspace modules,
    and warns that workspace files must be read and edited only through Vestibule (guest
    symlinks).
  - **Review:**
    - `critic` ran twice, and `verifier` checked every finding (several with live Docker
      trials).
    - Round 1 found a real Python-3.11 race: `asyncio.wait_for` could carry a cancelled
      run past shutdown untracked. The fix keeps it in the request's own task, with a
      regression test that fails on the old code.
    - Round 2's medium was refuted as stated: the race exists, but cleanup already removes
      the script before the late container can run anything. That guard is now
      deliberate and tested.
    - SIGTERM handling on Linux/macOS is deferred to M2.
  - **A test-hygiene leak** in a step-4 test (two `%TEMP%` dirs per suite run) was found
    and fixed.
  - **Totals:** 122 → 175 tests, ruff + mypy clean.
- **Plan files moved** (Victor's renames, committed with 7a; references fixed in 7b). The M1
  plans live in `docs/plans/M1/`, and each milestone gets its own folder. Every live
  reference now points there: README, PLAN, BUILD_LOG, these bullets, the plans' cross-references
  and the `src/` docstrings. The workflow docs name `docs/plans/<milestone>/<step>.md`. A new
  `tests/test_docs.py` fails on any stale link or `docs/…` path. Dated records (the CLAUDE.md
  changelog, `docs/reviews/`) stay verbatim.
- **D1 amended — 2026-10-14 (S7-D2, as approved 2026-10-11).** Guest code's blast radius is
  still the workspace, but a symlink a guest plants there is a real symlink on the host, and
  host tools that follow it reach past the workspace. `read_workspace` refuses links, the
  `run_code` description steers the model to Vestibule's own tools (7a), and the README now
  tells the user to treat the workspace like an untrusted download. The D1 bullet above
  stays as written. The amendment is in the contract.
- **`HOME=/tmp` — approved 2026-10-14.** The acceptance suite's criterion 9 found that
  `$HOME` (`/tmp/home`) never existed in the guest: `/tmp` is a fresh empty tmpfs, so writing
  `~/anything`, `cd ~` or `git config --global` failed. `HOME` is now `/tmp`, which always
  exists and counts against the same 64 MiB cap. Rejected: a second tmpfs at `/tmp/home`, and a
  `mkdir` shell wrapper in front of every guest. Contract §3 amended.
- **Step 7b — done 2026-10-14** (acceptance suite + docs; the live-agent check is next).
  - **A flaky 7a test, found by the first baseline run.** The test for runs cancelled at spawn
    failed once. Docker's own event log showed the product had done its job: the three
    late containers were each killed 34–67 ms after they started and were gone within 0.9 s.
    The test's poll had passed on its first, empty `docker ps`, taken before the containers
    existed, so its final check caught one mid-life. The test now waits until every `docker run`
    process has exited before an empty listing counts. It passed 5 times in a row afterwards.
  - **A small leak the gate turned up, older than step 7.** A run cancelled while
    its container was still being created could leave one empty temp dir behind. Docker
    Desktop re-creates a deleted bind-mount folder when such a late container starts.
    The task that waits for the `docker run` process now removes the dir again once that
    process has exited. The race occurred 4 times in the next 6 test runs, and no dir
    was left.
  - **The exam.** `tests/test_acceptance.py` holds one test per criterion, plus one that checks
    every isolation control from inside the guest. Each goes through the real tool handler
    against real Docker. All 14 pass. A few of the results:
    - a memory bomb is OOM-killed and the cgroup's own counter says so
    - forks are refused at 125 of 128 pids, and the classic bash fork bomb is contained
    - `/tmp` takes exactly 64 MiB, then reports "no space left"
    - the root filesystem refuses writes with `EROFS`, proof of the read-only mount itself
    - the guest sees uid 1000, all capabilities empty, `NoNewPrivs`, seccomp filtering, the
      exact cgroup limits, only `lo`, and none of the server's environment
  - **Docs.**
    - README: a Configuration table (all 15 `VESTIBULE_*` settings), a "Know the limits"
      section (not a VM; the workspace is writable and uncapped; treat it like an untrusted
      download; a hostile run can outlive its deadline; Podman is experimental), and the full
      set of isolation values.
    - A new short developer guide in `docs/GETTING_STARTED.md`. The M0 walkthrough is
      archived verbatim in `docs/archive/`.
    - The contract carries the acceptance record (criterion → test → what was observed).
  - **Criterion 14:** with Docker hidden from `PATH`, every Docker test skips with its reason and
    the rest pass (159 passed, 38 skipped). Proven on Windows only; Linux comes with M2.
  - **Review:** `critic` ran twice, and `verifier` checked every finding (none refuted).
    - Round 1 had one medium, which `verifier` lowered to low: "Know the limits" overclaimed.
      Under `container-degraded`, a hostile run is held only by the limits its result
      reported.
    - The other round-1 findings were low:
      - `test_docs` missed wrong-case paths, rejected plan forward references, and skipped
        `CLAUDE.md` and `tests/`
      - two tests could be fooled by a live Vestibule server sharing the machine
      - nothing without Docker guarded `HOME=/tmp`
      - three stale doc lines
    - All were fixed except config validation (`0` values, a Unicode digit stopping
      startup). That is now its own small fix in the known issues, because it changes config
      behavior.
    - Round 2: 4 low findings on the fixes, all fixed.
  - **Totals:** 175 → 197 tests, ruff + mypy clean, and no leftover containers or temp dirs
    before or after the run.
- **Live-agent check (plan §8 step 9) — passed 2026-10-14.** A fresh Claude Code
  session called the registered server over real stdio (step-7b code, `3c2dab9`). Every run
  reported `isolation: container`:
  - **Hello world** in python 3.12.15, bash 5.2.37 and node v22.23.3. The guest ran as uid
    1000 with `HOME=/tmp`, cwd `/workspace`, a writable workspace, and a root filesystem that
    refused writes (`Read-only file system`).
  - **Network:** `example.com` failed name resolution, `1.1.1.1:80` was `Network is
    unreachable`, and the only interface was `lo`.
  - **Memory bomb:** a run that kept allocating died with exit 137 after printing 192 MB. A
    second bomb ran in a child process: the child got SIGKILL, and the parent read
    `oom_kill: 1` from `memory.events` against `memory.max` = 256 MiB. So the memory cgroup
    did the kill, and the sandbox kept working.
  - **`read_workspace`:** it read back a file a run had written. It refused a link the run
    planted inside the workspace ("path traverses a symlink or reparse point"), and the
    listing labeled the link `symlink`. On the host the link was a real Windows
    `SymbolicLink`, as S7-D2 says. A follow-up run deleted the link and its directory, and
    the host workspace was empty afterwards.
  - **No containers left between runs**, checked with `docker ps -a` on the owner label.
  - **Shutdown:** the session was ended idle, as the plan specifies, and a detached watcher
    recorded the exits from outside. Claude Code's MCP log shows "Closing MCP server stdin"
    at 19:35:37.546 UTC. The server's Python process exited 0.11 s later, and the
    console-script launcher and the venv redirector within 0.18 s, all with exit code 0. A
    forced kill on Windows (Node's `kill`, `taskkill /F`) leaves exit code 1, so the server
    shut itself down on stdin EOF. Claude Code itself exited 0.96 s after closing stdin, and
    no Vestibule container was left. So Claude Code closes stdin first, and an idle server
    needs about a tenth of a second. How long Claude Code waits for a server that is slow to
    exit (a run in flight) was not measured (S7-D1 point 4). If it kills sooner than the
    server's ~2 s shutdown budget, the S7-D5 self-deadline still ends the container.
  - **Seen on the way (not M1 blockers):** the server reports the MCP SDK's version
    (`1.28.1`) as its own, because `Server("vestibule")` gets no `version=`. Logged in the
    BUILD_LOG known issues. Claude Code's `server/discover` probe produces a harmless stderr
    warning from the SDK, after which Claude Code treats the server as a legacy peer.
- **M1 accepted — 2026-10-14.** The acceptance suite passes all 14 criteria (step 7b), and
  the live-agent check above passed, shutdown included. Proven on a Windows host with Docker
  Desktop; the container backend first runs on Linux with M2. What carries forward is in the
  BUILD_LOG known issues: config validation and the version string (small fixes), the
  workspace's missing size cap and guest-made symlinks (documented), and the Linux SIGTERM
  path (M2).
- **Follow-up: config validation + server version — approved and done 2026-10-14.** The
  two small fixes above, in one change
  ([plan](plans/M1/M1-followup-config-version.md), approved by Victor the same day).
  - **Settings are validated now.** Before, a bad value was quietly swapped for the default
    (`VESTIBULE_MEM_MB=512m` meant 256), `0` was accepted (`MAX_CONCURRENT=0` refused every
    run with "retry shortly"), `²` crashed the server at startup, and a typo in
    `VESTIBULE_WORKSPACE_RO` meant a writable workspace. Now numbers must be positive whole
    numbers in ASCII digits (at most 18), on/off settings take `1/true/yes/on` or
    `0/false/no/off`, and blank still means unset.
  - **A bad config doesn't stop the server.** It starts, and every tool call returns a
    `Blocked:` message naming each bad setting, so the agent can tell the user what to fix.
    Two options were rejected. Refusing to start: the client only says "failed to
    connect". Starting with the defaults: that is the silent substitution again.
  - **Version:** the server reports `0.0.1`, not the SDK's `1.28.1`.
  - **Review:** two `critic` rounds found 6 low issues. `verifier` confirmed them all except
    one refuted half, and all are fixed. The notable one: the first guard against a
    5000-digit value leaned on Python's adjustable digit limit. It is now an explicit
    18-digit cap.
  - **Totals:** 197 → 240 tests, ruff + mypy clean.

---

## M2 — Native Linux isolation (in planning)

Goal: on Linux, run each sandbox with the kernel's own isolation (namespaces, a filesystem
jail, cgroup limits, seccomp) without root and without Docker, faster than the container
backend. Contract: [`docs/plans/M2/M2-native-warden.md`](plans/M2/M2-native-warden.md)
(draft; decisions N1–N5 wait on review and sign-off).

### Decision sign-offs

- **E1 — approved 2026-10-14.** One copy of the code, the Windows repo. Claude edits it and
  Victor commits from Windows as before. The Linux tests run in WSL Ubuntu from a venv kept
  outside the repo (`~/.venvs/vestibule`), so test temp dirs, the workspace and the rootfs
  live on WSL's own disk and see real Linux behaviour. Rejected: a second clone inside WSL
  (two copies to sync), and moving all daily work into WSL.
- **E2 — approved 2026-10-14.** M2's live-agent check uses Linux Claude Code installed
  inside WSL, because that is what a Linux user runs. Windows Claude Code launching the
  server through `wsl.exe` would give Windows users native isolation without Docker, but it
  is a separate path to design; it is noted as an M4 candidate.
- **E3 — approved 2026-10-14.** Plan shape like M1: one contract with a numbered step list
  now, and a detailed plan per step written just before that step is built, reviewed by
  `skeptic` and signed off by Victor.
- **Review choices on N4/N5 — approved 2026-10-14.** The `skeptic` review of the first
  draft (1 high, 10 medium, 13 low findings, all accepted; record in the contract's §12)
  raised three questions for Victor:
  - **seccomp is required for native.** A filter that blocks nested namespaces changes
    what hostile code can reach, which D3 calls hard. Without libseccomp, native is
    unavailable and the container backend is used. `CLAUDE.md`/`PLAN.md` will say
    "required for native" instead of "optional".
  - **The filter is an allowlist derived from Docker's default profile**, not a denylist.
    M1 already proved these images run under it, and syscalls a future kernel adds are
    denied by default.
  - **Ubuntu's restricted user namespaces** (stock 23.10+) are tested by injection in M2
    and on a real stock-Ubuntu VM in M4's fresh-machine test. The README lists which
    distros get native; no AppArmor profile is documented, because it would grant user
    namespaces to every `python3` on the system.
