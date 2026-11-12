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

## M2 — Native Linux isolation (✅ accepted 2026-10-31)

Goal: on Linux, run each sandbox with the kernel's own isolation (namespaces, a filesystem
jail, cgroup limits, seccomp) without root and without Docker, faster than the container
backend. Contract: [`docs/plans/M2/M2-native-warden.md`](plans/M2/M2-native-warden.md)
(signed off 2026-10-14, accepted 2026-10-31).

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
- **N1–N5 — approved 2026-10-14** (the revised contract as a whole):
  - **N1:** each run gets a fresh helper process instead of a fork of the server.
  - **N2:** the sandbox's filesystem is M1's pinned images, fetched once by
    `vestibule-mcp setup` without Docker.
  - **N3:** resource limits come from a systemd-delegated cgroup. A per-run scope is
    preferred unless the step-0 spike shows it adds more than 50 ms per run.
  - **N4:** seccomp is our own small libseccomp binding running a Docker-derived allowlist,
    required for native.
  - **N5:** selection on Linux takes the strongest verified guarantee first (native,
    container, native-degraded, container-degraded, then Blocked), and every run is
    checked, not only the probe.
  - Next: step 0, a throwaway spike that tests the kernel facts these decisions rest on.

- **Step-1 decisions — approved 2026-10-14** ([plan](plans/M2/M2-step1-linux-baseline.md)).
  Building the Linux venv showed that a fresh install today gets mcp 2.3.0, whose new server
  API stops Vestibule from starting, on any platform. The skeptic, confirmed by the
  verifier, showed that real clients allow far less shutdown time than M1 assumed: Claude
  Code on Linux sends `SIGINT`, then `SIGTERM` 100 ms later; on Windows it allows 300 ms.
  Today's server hangs on that `SIGINT`.
  - **S1-D1 — a version range plus a constraints file.** `pyproject.toml` states what users
    get (`mcp>=1.28.1,<2`). A constraints file frozen from the Windows venv pins every
    package exactly for both dev venvs, and both gates check it. Moving to mcp 2.x is a
    planned task before the PyPI release. Rejected: migrating now (mid-M2 churn in the
    protocol layer) and a lock-file tool (more than step 1 needs).
  - **S1-D5 — shutdown hands off and exits fast on Linux; Windows keeps M1's path
    (revised the same day).** On Linux and other POSIX systems, any trigger (stdin closed,
    `SIGINT`, `SIGTERM`, `SIGHUP`) runs one synchronous path. It kills this server's
    `docker run` processes, starts a detached cleaner that removes its containers and temp
    dirs (with later sweeps for late starters), and exits within ~50 ms, inside every
    client's window. The first version used the same hand-off on Windows too. The second
    skeptic review showed the cleaner dies there with the Python SDK's kill-on-close Job
    Object on every session. A measurement showed Claude Code's Windows server chain sits in
    such jobs too. So Windows keeps M1's in-process cleanup, which fits the Python SDK's
    2 s. Claude Code's 300 ms Windows window stays a documented gap: the container's
    self-deadline ends it. A container-side liveness lease is logged as the later fix for
    every platform. Rejected: adding the lease now (changes M1's container profile
    mid-step) and keeping the hand-off on Windows (worse than M1 for Python SDK clients).

- **Step-2 decisions — approved 2026-10-24** ([plan](plans/M2/M2-step2-rootfs.md), v2 after
  a `skeptic` review whose 13 findings were all accepted). Before planning, the two pinned
  images were downloaded and inspected: about 123 MB to download and ~350 MB on disk, plain
  gzip layers, and no whiteouts, device files or entries that pass through a symlink.
  - **S2-D1 — `vestibule-mcp setup`.** It fetches and unpacks both images. Any other
    argument prints a warning and the server starts as before; refusing to start was
    already rejected in M1, because the client then only says "failed to connect".
  - **S2-D2 — refuse what the images don't use** (amends the contract's §6). Whiteouts,
    device files and a folder replaced by a file make setup fail with the entry named,
    instead of being supported. Supporting whiteouts means deleting folders inside an
    untrusted tree, the riskiest code in the step, for a feature neither image uses. A
    re-pin now runs setup, so a future image that needs one fails before it ships.
  - **S2-D9 — overlapping folders refuse native, not the server** (amends the contract's
    §7). If the workspace and the image folder overlap, setup (and, from step 6, native
    selection) refuses to use them, but the server starts and the container backend still
    works. A config error would have blocked every tool for working M1 setups such as
    `VESTIBULE_WORKSPACE=~`.
- **Step-3 decisions — approved 2026-10-25** ([plan](plans/M2/M2-step3-warden-core.md),
  v3).
  - **Sign-off:** Victor said to proceed with the build after a second `skeptic` review,
    which used the new Karpathy check.
  - **The two reviews:** 6 findings, then 13 (one high: the guest could open the server's
    terminal through `/dev/tty`). All were accepted.
  - **Before planning,** a throwaway prototype ran against the real rootfs on both Pythons.
    It found eight problems the spike's design would have shipped with, among them a helper
    launch that couldn't find its own Python install, lost imports after the jail closed, and
    a `/proc/keys` that listed the user's kernel keyrings.
  - **S3-D1 — the script lives in a per-run memory folder at `/sandbox`** (amends the
    contract's §4.2). Init writes it there and makes the folder read-only, so a run leaves
    nothing on the host, even when the server is killed. The contract had a host temp folder.
  - **Contract changes that came with the sign-off:**
    - how the helper is started (N1);
    - the label for a run with no status report, `none` (N5);
    - the kill order without cgroups (N8);
    - Docker's `/proc` masks, loopback up, and no controlling terminal (N9).
- **Step-4 decisions — approved 2026-10-26** ([plan](plans/M2/M2-step4-cgroups.md), v3).
  - **Sign-off:** Victor approved v3 after one `skeptic` review (11 findings, all accepted)
    and a Karpathy re-check that he asked for (3 more gaps, fixed in the plan).
  - **Before planning,** a throwaway prototype ran the whole step in WSL. Per-run systemd
    scopes add about 2 ms to a run, and the memory, process and CPU limits all held.
  - **S4-D1 — no "already inside a delegated cgroup" path** (amends N3). Every run gets its
    own systemd scope, or runs without limits and says so. The path would have needed the
    server to move itself, and no host we test has it.
  - **S4-D2 — no `cgroup.kill`** (amends N8). Step 3's stop already kills everything in the
    sandbox, stopped processes included, and it is the one signal that tells the server the
    sandbox is really gone.

### Build steps (8 planned: 0–7)

- **Step 0 — spike, done 2026-10-14 (no repo code;
  [report](plans/M2/M2-step0-spike.md)).** Throwaway scripts in WSL built the whole sandbox
  by hand and tried it out.
  - **It works:** python, bash and node run with no host files, no network, an empty
    capability set, `NoNewPrivs`, Docker's seccomp allowlist, and PID 1 locked away from
    the guest. Blocked calls come back as "not permitted" and are reported by name.
  - **Containment:** a memory bomb is OOM-killed inside its own cgroup. A fork bomb stops
    at 128 processes. A stopped or looping guest is killed in ~2 ms. Killing the server
    mid-run leaves nothing behind (gone within 12 ms).
  - **Cold start:** about 75 ms per run with the per-run systemd scope, against ~387 ms for
    a bare `docker run`. The per-run scope adds ~21 ms, under the 50 ms line, so N3 uses it.
  - **No decision changed.** Six corrections went into the contract as amendments. The
    main one: the helper must not share init's mount namespace, because `pivot_root` pulled
    it into the sandbox too.
  - **Left for the step-5 plan:** Node probes `io_uring` on every start, so it must be
    denied silently. Docker allows `ptrace`, which our plan denies; Victor decides in step 5.
  - **Benchmark expectation:** on this host native is ~5× faster than a bare `docker run`,
    not the 10–50× `PLAN.md` hoped for. The README will report measured numbers only.
- **Step 1a — Linux baseline, done 2026-10-15
  ([plan](plans/M2/M2-step1-linux-baseline.md)).** No server code changed.
  - **Pinned:** `pyproject.toml` now caps `mcp` below 2, and a constraints file pins every
    package exactly, so every dev venv is identical on every platform and Python version.
    A fresh install had been pulling mcp 2.3.0, which can't run Vestibule.
  - **Checked:** `check_env.py` refuses a venv that differs from the constraints, runs the
    wrong Python, or is bound to a different checkout.
  - **Linux gate:** the full suite runs on the system Python 3.14, and the Docker-free and
    stdio end-to-end tests on a uv-installed 3.11. Every pytest run is time-bounded, and any
    unexplained skip fails it. The gate agent now runs Windows, then Linux.
  - **Review:** the `critic` found 11 issues (3 medium), all confirmed by `verifier` and
    fixed before the step was called done.
- **Step 1b — POSIX shutdown hand-off, done 2026-10-15
  ([plan](plans/M2/M2-step1-linux-baseline.md) S1-D5; committed with 1a in `d811021`).**
  - **The problem:** Claude Code on Linux ends a server with `SIGINT`, then `SIGTERM` 100 ms
    later, then `SIGKILL`. The old server hung on that `SIGINT`, so a run in flight left its
    container alive until the container's own deadline.
  - **Now:** on Linux, closing stdin or any of those signals runs one quick path. The server
    kills its `docker run` processes, starts a small detached cleaner that removes its
    containers and temp dirs (and sweeps again for late starters), and exits within
    milliseconds. Windows keeps M1's in-process cleanup, because a detached cleaner dies
    with the Windows client's job there.
  - **Found along the way:** a container that starts late can re-create its deleted run dir
    as root, which the user can't delete from the shared `/tmp`. Run dirs now live in a
    private user-owned folder on Linux.
  - **Review:** two `critic` rounds (2 medium + 3 low, then 1 medium + 3 low), all confirmed
    by `verifier` and fixed. Among them: the cleaner failed silently in some installs, and
    naive-backend guests could outlive the server.
- **Step 2 — rootfs and `vestibule-mcp setup`, done 2026-10-25
  ([plan](plans/M2/M2-step2-rootfs.md); committed in `2af2c0c` and `9197a55`, review fixes in
  `4c8afcf`).**
  - **Now:** on Linux, `vestibule-mcp setup` downloads the two pinned images straight from
    Docker Hub, with no Docker needed. It checks every byte against the pin and unpacks each
    image into a folder the native sandbox will use as its `/` (step 3). Both images take
    about 10 s and 357 MB, and a second run does nothing. The server doesn't use them yet.
  - **How we know the unpacker is right:**
    - a test compares every entry of both trees with Docker's own `docker export` of the
      same image;
    - attack tests (symlinks and hardlinks that try to escape the tree) run next to a
      sentinel folder that must stay untouched;
    - deliberately broken versions of the unpacker were run against those tests to check
      they fail.
  - **Review:** two `critic` rounds found 11 issues, all fixed. The main ones:
    - folders in the tree (such as `/var/tmp`) were left writable by every local user. Old
      trees are now replaced on the next setup;
    - a registry could point the token request at a local file.
  - **Karpathy re-review (2026-10-25):** the whole step looked at again for code nothing
    asked for, and for tests that would pass on broken code.
    - It found 8 small things, all confirmed and dealt with: two weak tests were
      strengthened, and five pieces nobody needed were removed.
    - One check was kept on purpose: the repository-name check, which stops a typo in an
      image name from silently changing the request.
    - The size overrun of `registry.py` is now explained in the plan instead of ignored.
- **Workflow change — the Karpathy rules (Victor, 2026-10-24 and 2026-10-25).**
  - **2026-10-24:** four coding rules from Andrej Karpathy's guidelines joined the loop:
    - think before coding;
    - simplicity first;
    - surgical changes;
    - goal-driven execution.

    They live in `CLAUDE.md`'s "Coding discipline" section, with worked examples in
    `.claude/KARPATHY_EXAMPLES.md`. The main session reads them before each plan and checks its
    own diff against them before the gate.
  - **2026-10-25:** `skeptic` and `critic` check plans and diffs against the rules too. Step 2's
    build reviews had checked correctness only, so step 2 got the re-review above.
- **Step 3 — warden core, done 2026-10-25
  ([plan](plans/M2/M2-step3-warden-core.md); commit `1ef1b46`).**
  - **Now:** `NativeBackend` runs a snippet in a fresh sandbox built from the step-2 image
    folders:
    - its own user, process, mount, network and other namespaces;
    - a filesystem jail where only the workspace, a read-only script and small memory folders
      are visible;
    - a process 1 the guest can't read, signal or take over;
    - no terminal and no network, except its own `localhost`.

    A run that overruns is stopped with nothing left behind, even a guest that has stopped
    itself. If the server dies, the whole sandbox dies with it.
  - **Not usable from the MCP server yet.** Every run still reports `isolation: none`, because
    the seccomp filter that native requires arrives in step 5. Resource limits come in step 4,
    and the server starts choosing native in step 6.
  - **How we know:**
    - 27 Linux tests run real sandboxes on both Pythons;
    - 43 offline tests cover the verdict rules on every platform;
    - 14 deliberately broken versions of the code each turned a test red.
  - **Review:** two `critic` rounds found 6 issues, all confirmed and fixed. The main ones:
    - the helper's own deadline never fired;
    - the mount check would have accepted a sandbox that was missing the `/proc/keys` mask;
    - a client that stops reading the server's log could have blocked that deadline.
- **Step 4 — resource limits, done 2026-10-26
  ([plan](plans/M2/M2-step4-cgroups.md); commit `8b1de5e`).**
  - **Now:** each native run gets its own temporary systemd scope. The guest gets 256 MiB of
    memory with no swap, 128 processes and 75 % of one CPU, and the server reads every limit
    back before it counts it.
    - A memory bomb is killed, and the result says it ran out of memory at its limit.
    - A fork bomb stops at the process limit.
    - systemd's own deadline ends a run even if the helper itself is broken.
  - **Without systemd,** runs still happen and say which limits are missing. A process-count
    backstop keeps a fork bomb from filling the user's process table.
  - **Still `isolation: none`** until step 5's seccomp filter. With that check set aside, a
    run now reports `native`.
  - **How we know:**
    - 17 new Linux tests run real sandboxes with limits, on both Pythons;
    - 25 new offline tests cover the rules;
    - 28 deliberately broken versions of the code each turned a test red.
  - **Review:** `critic` found 8 issues, all confirmed or plausible, all dealt with. The two
    real bugs:
    - on a machine without systemd and with a low process limit, every run was refused;
    - on an old cgroup-v1 machine, every run would have waited 5 s for nothing.
- **S5-D1 signed off (ptrace), 2026-10-26.** The step-5 plan asked one question: should the
  sandbox's seccomp filter let a program use `ptrace` (debuggers, `strace`, profilers)?
  Victor chose to **follow Docker and allow it** on modern kernels. Inside the sandbox a program
  can only ever trace its own child processes — it has no privileges, can't reach process 1, and
  can't gain any — so allowing it opens no new way out, and it keeps the native sandbox behaving
  like the container one. The stricter alternative (deny and report it) stays a one-line switch.
- **Step 5 — seccomp, done 2026-10-27
  ([plan](plans/M2/M2-step5-seccomp.md); commit `06bd899`).**
  - **Now:** every native run loads a syscall allowlist — the set Docker ships, adapted for a
    no-privilege sandbox — right before the program starts, and the program inherits it. This is
    the last piece the native warden was waiting on: **a run now reports `isolation: native`**
    for real (with step 4's limits), instead of `none`.
    - Dangerous calls (mounting, new namespaces, loading kernel code, raw devices, the keyring,
      a non-standard network family like the one that reaches the Windows host) are blocked, and
      the ones on a named list are reported back to the model by name (`blocked syscalls: …`) so
      it can adapt. A chatty program can't flood that report: after 256 it stops and the rest are
      blocked silently.
    - The filter is our own small binding over the system's `libseccomp`; the Docker profile is
      vendored into the package and locked to a checksum, so it can't change without a review.
  - **How we know:**
    - the whole mechanism was first run by hand on both Pythons (3.14 and 3.11), with the same
      result: a hello runs as `native` and reports nothing; mounting, new namespaces, the
      keyring and the host-network family are each blocked and named; a storm of 60,000 blocked
      calls comes back in about two seconds with one name and a "stopped" note;
    - the Linux test suite grew to 480 tests (0 skipped); the Windows suite stays green;
    - the real-sandbox tests moved onto the real filter (they used a stand-in before), and new
      tests cover the binding, the reported set, the storm, and the checksum;
    - 5 deliberately broken versions each turned a guarding test red (a lost or mis-read
      reporting channel, no de-duplication, the filter never loaded, the ptrace switch flipped).
  - **Review:** `skeptic` on the plan said "rework needed" and its three findings reshaped the
    design before any code (the reporting channel's framing on an early setup failure; a test
    that would have hung; a 3.14-only crash from building the filter in the wrong process).
    `critic` on the finished diff found no real defects, only one small "dead branch" note on the
    kept ptrace switch.
- **Step 6 plan signed off, 2026-10-27
  ([plan](plans/M2/M2-step6-selection.md)).** Step 6 makes native isolation reachable from the
  MCP server: on Linux, `auto` tries native first and falls back in the contract's order
  (a full container beats a native sandbox missing its limits). `skeptic` found 14 issues, 6 of
  them medium, and every one changed the plan. The main ones:
  - a degraded native choice could have stuck for a whole session even after Docker came up;
  - the reason limits were missing would only have reached the server log, not the model;
  - three messages earlier steps promised had been dropped.

  Victor decided two questions, both as recommended:
  - **S6-D1:** the probe that picks native is a normal sandboxed run. Every run already has the
    server check the sandbox from facts the kernel reports, so the probe doesn't re-check facts
    the untrusted program prints about itself.
  - **S6-D10:** the test-only switch that skipped the seccomp filter is **deleted**, not kept.
    The tests that used it now run the real filter, as users do.
- **Step 6 — native isolation reachable from the server, done 2026-10-27
  ([plan](plans/M2/M2-step6-selection.md); commit `3cf3024`).**
  - **Now:** on Linux, a user who ran `vestibule-mcp setup` gets `isolation: native` from the
    MCP server. `auto` tries native first. If native is missing its limits (no systemd user
    manager), a full container wins; a container that's only down for now is asked again every
    30 s, so starting Docker later still upgrades the session.
  - **Messages that name the fix:** the exact `setup` line, a digest-pinned image, Ubuntu's
    user-namespace restriction, a missing libseccomp, a hidden `/proc`. A run missing its limits
    says why, for example "— no systemd user bus".
  - **`VESTIBULE_BACKEND`** takes `auto`, `native`, `container` and `naive`; native and container
    runs share one concurrency limit. The test-only seccomp switch is gone from the code.
  - **How we know:**
    - the Linux suite grew to 539 tests (0 skipped) and Windows stays green;
    - the server, started with only the six variables the MCP SDK passes, still picks native;
    - closing the server mid-run leaves nothing behind;
    - 29 deliberately broken versions each turned a test red. One of them showed a test that
      couldn't fail, now fixed.
  - **Review:** `critic` ran twice, each round checked by `verifier`, and every finding was
    confirmed and fixed. The main ones:
    - asking again whether Docker was up meant probing native again too, which could block
      every call for 30 s when the run limit was reached;
    - a Docker that was half-started, or images not yet pulled, would have left the session
      without resource limits until a restart.
- **Step 7 plan signed off, 2026-10-28
  ([plan](plans/M2/M2-step7-acceptance.md)).** The last step of M2. It has an exam (one test
  per acceptance criterion, through the same entry point an agent uses), a speed benchmark
  native against container, a live session with Linux Claude Code in WSL, and a pass over
  every doc written before M2 that is now wrong. `skeptic` found 21 issues, 8 of them medium,
  and every one changed the plan. The main ones:
  - one criterion was never tested through the server itself;
  - the benchmark had no guard on what it measured;
  - the design doc kept claims the build had already disproved.

  Victor decided three questions, all as recommended:
  - **S7-D1:** two parts, as in M1. 7a is the exam and the benchmark. 7b is the live checks
    first, then the docs, with "M2 accepted" written last.
  - **S7-D2:** the "foreign-architecture calls are killed" criterion rests on step 5's
    filter-level tests, and its wording says exactly what they cover.
  - **S7-D3:** Linux Claude Code goes into WSL through its native installer, with no Node.
- **Step 7a — the exam and the benchmark, done 2026-10-31
  ([plan](plans/M2/M2-step7-acceptance.md) §8; commit `6ada758`).**
  - **Now:** one test per M2 criterion goes through the same entry point an agent's tool call
    reaches, on a real Linux host: hellos in three languages, no host files, no network, memory
    and fork bombs contained, the CPU and `/tmp` limits, timeouts with nothing left behind,
    blocked syscalls named, the guest's own view, a server killed mid-run, four runs at once,
    and a rootfs that vanishes under a running server. All 14 pass, with Python 3.14 and 3.11.
  - **The speed number the README will quote,** measured on this machine through the server
    itself: a native run takes about 80 ms (p95 about 110 ms), a container run about 390 ms.
    The container path here goes through Docker Desktop's VM, which a Docker Engine user on
    Linux doesn't pay, so there is no "N× faster" headline.
  - **How we know the tests can fail:** 10 deliberately broken versions of the code each turned
    the right test red.
  - **What it caught:** the test draft had three bugs of its own. Its "nothing left behind"
    check looked at an empty list, so it could never fail; one check proved itself in a
    circle; and one test flaked. All three are fixed.
  - **Review:** `critic` found 1 medium and 8 small issues, and `verifier` confirmed 8. The
    medium one: one of the plan's broken versions could never have been caught by the test it
    named, because a second safety net hides it. The plan's list now says which test guards
    what.
- **Step 7b — the live checks and the docs, done 2026-10-31
  ([plan](plans/M2/M2-step7-acceptance.md) §8; commit `f318e0c`).**
  - **Linux Claude Code, live.** Claude Code 2.1.316, installed in WSL with its native
    installer, ran a session with only Vestibule's two tools available. Every run reported
    `isolation: native`:
    - hellos in python, bash and node;
    - a TCP connection to `1.1.1.1` failed with `Network is unreachable`;
    - a memory bomb died with exit 137 and the note that the kernel killed it at its 256 MiB
      limit;
    - reading a fake key planted at an absolute path in the home folder got `No such file or
      directory`;
    - `read_workspace` listed the file the bash hello had written.

    When the session ended, the server's last log line was `vestibule shutdown (SIGINT):
    nothing to clean up`, as step 1b predicted, and no process or scope was left.
  - **Windows Claude Code, the shutdown re-measure.** A long container run, watched from
    outside. Pressing Ctrl+C cancels the call before Claude Code exits, and the container was
    gone by the next check, under a second later. Closing the terminal window mid-run ends the session with the call still
    going, and the container was gone within 1.43 s, more than two minutes before its own
    deadline. The race the known issue describes didn't happen in either case, but one sample
    can't rule it out, so the issue stays open for its planned fix.
  - **Seen on the way:** after an interrupted call, Claude Code reports Vestibule's connection
    as dropped, because the MCP SDK answers the cancelled call and Claude Code doesn't expect
    that answer. Nothing leaks, and `/exit` still shuts the server down. Logged as an upstream
    known issue.
  - **Docs:** the README explains native isolation on Linux (what it is, what `auto` picks, the
    speed numbers, which distros should get it and why, and the new limits), and Linux setup no
    longer asks for Docker. `ARCHITECTURE.md`'s warden part is rewritten to match what was
    built, and `CLAUDE.md`, `PLAN.md`, `GETTING_STARTED.md` and the contract are brought up to
    date. The contract has its acceptance record, criterion by criterion.
  - **Golden rule 2's explanation reworded — approved by Victor 2026-10-31.** It said the user
    namespace maps your uid to root inside; the build keeps your own uid (N6). The rule itself,
    never needing real root, is unchanged.
  - **Review:** `critic` found 13 issues in the docs, and `verifier` confirmed all 13: one
    medium, the rest small, all wording. The medium one: the docs said a sandbox's guest is
    never root, but nothing stops someone starting the server itself as root, and then the
    guest is root too (without any capabilities). The docs now say so, and refusing a root
    server is a known issue for its own small step. Others: the design doc said a failed
    seccomp check stops the run before it starts (the server judges it after the run, and the
    result then says `none`), and a few claims were worded as complete lists or as more precise
    than the measurements.
  - **A test that depends on the machine.** The first gate run failed one Linux test, which caps
    the user at 100 processes. The kernel counts every process of that user across all of WSL,
    including ones in other distros that Ubuntu can't see: 102 then, with a leftover Claude Code
    session open, and 76 after Victor closed it, when the gate passed. Logged as a known issue;
    the test needs its own small fix.
  - **Totals:** 553 tests on Linux with none skipped, 412 plus the 121 Linux-only skips on
    Windows; ruff and mypy clean.
- **M2 accepted — 2026-10-31.** Every one of the 17 criteria has its evidence: the exam
  through the server (7a), the benchmark, both platform gates, and the live session with Linux
  Claude Code (7b), shutdown included. Proven on WSL2 Ubuntu 26.04 (Microsoft's kernel 6.6,
  systemd 259); other distros are expected from their prerequisites, and M4's fresh-machine
  test runs a stock Ubuntu 24.04 VM. What carries forward is in the BUILD_LOG known issues: a
  server started as root, the process-limit test, the SDK's answer to a cancelled call, and the
  Windows shutdown race (a liveness lease); and `SECURITY.md` in M4.
- **Follow-up: four small fixes before M3 — plan signed off 2026-11-01**
  ([plan](plans/M2/M2-followups.md), v2 after the `skeptic` review; `verifier` confirmed all
  seven findings).
  - **What's in it:**
    - the process-limit test stops depending on the user's task count;
    - a server started as root;
    - an `isolation:` line on the two `run_code` results that lacked one;
    - two stale doc lines.
  - **F-D1 — Victor chose option 1: a server started as root runs no code.** On Linux and
    macOS, every isolating backend refuses it with a `Blocked:` message: start Vestibule as
    your own user, then restart. Only the explicit dev-only naive backend, which claims no
    isolation, still runs. v1 had recommended refusing native only. The review showed that
    the container then runs as `--user 0:0` and still says `isolation: container`, against
    M1's signed-off D3 and the README's "non-root user". So the false claim would only have
    moved. The other option, running as root and labelling every result degraded, was
    declined.
- **Follow-up done 2026-11-01** ([plan](plans/M2/M2-followups.md) §7; commit `c5fe2a2`).
  - **A server started as root runs no code.** The first `run_code` call says why and how to
    fix it. Only the dev-only naive backend still runs.
  - **Every `run_code` result now states its isolation.** Before, two results had no
    `isolation:` line: the one when the server's own time limit fires, and the one when
    something unexpected breaks. Both now say `isolation: none`, because nothing confirmed what
    protected the run.
  - **The test that broke the 7b gate** no longer depends on how many processes you have open.
    It failed for the old version with 40 extra sleeping processes and passed for the new one.
  - **Changed during the build:**
    - The planned second root check, in native's per-run verdict, was dropped. It couldn't tell
      real root from root inside a user namespace mapped to your own uid, and two existing tests
      run the sandbox exactly that way. The check at selection covers every path the server
      takes.
    - The root check's Windows guard was rewritten, because tests that pretend to be on Linux
      reached a function Windows doesn't have.
  - **Review:** `critic` found 1 low issue, which `verifier` confirmed. The selection unit tests
    failed when pytest itself ran as root. A small test fixture fixed that, and they now pass
    as root. The docs now say to run the checks as your own user.
  - **Totals:** 553 → 562 tests on Linux, none skipped; ruff and mypy clean.

## M3 — Audit log + resource reporting (✅ accepted 2026-11-07)

Goal: every `run_code` result carries real usage numbers, every call leaves a record in a
local audit log, and `vestibule-mcp audit` shows what agents have been doing. Contract:
[`docs/plans/M3/M3-audit-usage.md`](plans/M3/M3-audit-usage.md) (signed off 2026-11-01,
accepted 2026-11-07).

### Decision sign-offs

- **The M3 contract — signed off 2026-11-01 (v2).** `skeptic` reviewed v1 and `verifier`
  confirmed all 13 findings; v2 takes every one (the contract's §7). Victor chose option 1 on
  each open decision:
  - **A1 — usage numbers only from outside the sandbox.** Native runs report cpu, peak
    memory, peak processes and the limits they hit, read from the run's cgroup by the helper,
    timed-out runs included. Container runs report wall time and say why the rest is missing:
    the numbers could only come out through the guest's own output, which it can fake. This
    amends PLAN.md's "in every result" for containers. Declined: numbers measured inside the
    container, labelled unverified.
  - **A2 — the record keeps the code up to 4 KiB, with its SHA-256 and size, and only the
    sizes of the output.** Declined: the hash alone, and the full code up to 256 KiB.
  - **A3 — a failed audit write doesn't stop the run.** The result says "audit: not recorded"
    or "not confirmed". Declined: refusing to run without a confirmed record.
  - **Out of M3:** workspace quotas (they need root), per-run workspace change reports,
    automatic retention, tamper-proofing, and `%TEMP%` leftovers (moved to M4).

### Build steps (5 planned)

- **Step 1 plan signed off, 2026-11-02 ([plan](plans/M3/M3-step1-usage.md), v2).** Step 1
  puts real numbers in every result: wall time from the server, and for native runs the cpu,
  peak memory, peak processes and the limits they hit, read by the helper from the run's
  cgroup after the whole sandbox has exited. A timed-out run waits at most 1 s more for them.
  `skeptic` found 13 issues and `verifier` confirmed 12 and rated 1 plausible; every one changed
  the plan. The main ones:
  - the kernel adds throttled time up across CPUs (8.7 s in a 2 s run), so throttling is shown
    as periods, which can't exceed the run;
  - the pids limit counts threads too, so refusals say "new processes or threads";
  - the numbers include the sandbox's own setup and the file cache, so the result says so.

  Victor approved all seven places where the plan changes the contract's wording (U1–U7):
  MiB instead of MB, field names in the "not measured" note, the two labels above, one note
  covering all three numbers, three of the six new result fields now and three in step 2, and
  no usage line on the two results that have no run result.
- **Step 1 — usage numbers in every result, done 2026-11-02
  ([plan](plans/M3/M3-step1-usage.md) §9; commit `40f63be`).**
  - **Now:** every `run_code` result that has a run behind it carries a `usage:` line.
    - **Native runs** read, for example, `usage: wall=84ms, cpu=31ms, mem_peak=11MiB,
      pids_peak=2 (the whole sandbox, including its setup and file cache)`. When a limit was
      hit, a second line follows, such as `limits hit: pids (4113 new processes or threads
      refused), cpu (throttled in 18 of 20 periods of 100 ms)`.
    - **Where the numbers come from:** the helper reads them from the run's cgroup after the
      whole sandbox has exited, so the guest can't write them.
    - **Timed-out runs** get them too: the server waits at most 1 s more, and they now name
      their blocked syscalls as well.
  - **What a result says when numbers are missing:**
    - container runs report `wall` and say the rest can't be read from outside a container;
    - native runs without a cgroup, or on a kernel without a counter, say which number is
      missing and why;
    - a native run whose hard controls weren't confirmed shows no numbers at all.
  - **How we know:**
    - the gate's Linux suite grew to 615 tests (0 skipped), plus one test added after it, and
      Windows stays green;
    - 18 deliberately broken versions each turned a test red, among them a 5 s wait and a
      wall time that counts selection.
  - **Review:** `critic` ran twice, each round checked by `verifier`. The medium finding was
    that a run whose hard controls weren't confirmed still showed numbers, when those controls
    are what keep the guest from forging them. Fixed, with a test through the real run path.
- **Step 2 plan signed off, 2026-11-03 ([plan](plans/M3/M3-step2-audit-writer.md), v2).**
  Step 2 writes the audit log: every `run_code` call that reaches the handler leaves a record
  in `~/.vestibule/audit/`, written by one background thread so a stuck disk never stalls the
  server. A record that fails to land doesn't stop the run; the result gets an `audit:` line
  saying so. `skeptic` found 18 issues, none high; `verifier` confirmed 14 and rated 3
  plausible, and every one changed the plan. The main ones:
  - tests use an in-memory stand-in for the log, so no test writes to the real folder or waits
    on a disk;
  - a record that is never waited for (a cancelled call's) still logs a warning if it fails;
  - output sizes are the raw bytes collected, so a run stopped for flooding its output can't be
    mistaken for an out-of-memory kill.

  Victor approved all four places where the plan changes the contract's wording (W1–W4):
  - a call that ends during backend selection (refused, cancelled or failed) gets a `blocked`
    record with that outcome;
  - one wait of at most 2 s per call, after the run, instead of up to 4 s;
  - raw byte sizes plus an `output_capped` flag;
  - a machine with no home directory runs as today, its records reported `not recorded`.

  Also stated and accepted: `VESTIBULE_WORKSPACE=~/.vestibule` becomes a config error, because
  that workspace would contain the audit folder.
- **Step 2 — the audit log, done 2026-11-03
  ([plan](plans/M3/M3-step2-audit-writer.md) §9; commit `fb26ba1`).**
  - **Now:** every `run_code` call that reaches the handler leaves records in
    `~/.vestibule/audit/` (`VESTIBULE_AUDIT_DIR`), one JSONL file per server process.
    - A call that ends before a backend is chosen gets a `blocked` record. Every other call
      gets a `start`, with the code up to 4 KiB, its hash, the backend, the verdict, the image
      and the MCP client. It also gets an `end`, with the outcome, the exit code, the usage
      numbers, the backend's own run id and the image that ran.
    - The outcome is one of `ran`, `timed_out`, `not_run`, `refused`, `failed` or
      `cancelled`. A call cancelled with Ctrl+C still leaves its `end`.
    - One background thread writes the file, so a stuck disk never stalls the server. Each
      call waits at most 2 s for its records, after its run.
    - A record that doesn't land never stops the run: the result gets an
      `audit: not recorded (…)` or `audit: not confirmed (…)` line above `isolation:`.
    - The audit folder must be outside the workspace and the rootfs folder, or every call is
      `Blocked:` with the fix.
  - **How we know:**
    - Linux: 693 tests on Python 3.14, 0 skipped. Windows: 533, plus 140 Linux-only skips.
    - The real stdio test now reads the four calls' records back from its own audit folder.
    - 28 deliberately broken versions each turned a test red.
  - **Review:** `critic` ran twice, each round checked by `verifier`.
    - **The medium finding:** the folder check ran at startup and could crash the server on
      an offline network drive. Now a path that can't be resolved is compared as written.
    - **The low ones:** the setup command the server prints now names the audit folder. A
      file handle gone bad is reopened on the next record. The writer's newline repair now
      reads the end of the file after any failure.
    - **Not fixed, recorded:** the check can still block startup on a dead hard-mounted NFS
      share.
  - **Found by the gate:** the in-memory audit stand-in for tests first used pytest's
    `monkeypatch`, which reordered teardowns and broke six native tests on Linux. It now
    restores the real log by hand.
- **Step 3 plan signed off, 2026-11-04 ([plan](plans/M3/M3-step3-audit-command.md), v2).**
  Step 3 adds `vestibule-mcp audit [N]`, which prints the last N calls from the audit log, one
  line each, with every value cleaned so code an agent ran can't drive the terminal. `skeptic`
  found 12 issues (2 medium); `verifier` confirmed all 11 checkable claims, and every one
  changed the plan. The main ones:
  - a hand-edited record can't crash the command or flood a row: only strings and numbers are
    printed, each with a length cap;
  - the folder setting goes through the same helper as the server's check, so both give the
    same messages.

  Victor approved all five places where the plan changes the contract's wording (R1–R5):
  - every control and invisible formatting character is escaped, not only the contract's list;
  - the code's first non-blank line is shown, `(blank)` when there's none;
  - a refused or failed call shows its wall time and code as well as its reason;
  - stdout keeps its own encoding, with escapes for what it can't encode, instead of forced
    UTF-8;
  - denied syscalls are shown when there are any.
- **Step 3 — `vestibule-mcp audit`, done 2026-11-04
  ([plan](plans/M3/M3-step3-audit-command.md) §9; commit `a9dfc09`).**
  - **Now:** `vestibule-mcp audit [N]` prints the last N calls (default 20) from every audit
    file, merged by time, one line each.
    - Each line has the time, language, outcome, isolation, exit code, `wall`, `cpu` and
      `mem_peak`, any denied syscalls, and the code's first line. A refused or failed call also
      shows its reason. A call with no end says it may still be running, or its record was lost.
    - Every value from a record is printed escaped: controls, invisible formatting characters
      and line separators. Only strings and numbers are printed, each cut to a cap. Output that
      isn't UTF-8 is pure ASCII.
    - Damaged lines are skipped and counted. A file that can't be read is named, and the rest
      are still shown. A folder the server would refuse (inside the workspace or the rootfs
      folder) gets a warning.
  - **How we know:**
    - Linux: 781 tests on Python 3.14, 0 skipped. Windows: 620, plus 141 Linux-only skips.
    - One test reads a real audit file while the writer still holds it open. Two run the
      installed command: over a cp1252 pipe, and with a reader that quits early.
    - 39 deliberately broken versions each turned a test red.
  - **Review:** `critic` ran twice, each round checked by `verifier`, which confirmed all 16
    code findings.
    - **The main ones:** the 60- and 100-character caps had no test through the command. A
      legacy 8-bit terminal charset (KOI8-R, or cp1252 on a Windows pipe) puts printable
      characters on bytes a terminal reads as control codes, so output that isn't UTF-8 is now
      pure ASCII. One looping symlink stopped the whole listing.
    - **Left for Victor, approved 2026-11-05:** R4 as tightened, and the overlap warning,
      which reads two settings the plan said it wouldn't.
  - **Found by the gate:** one teardown error in a native test on the first Linux run. It didn't
    come back: 20 of 20 alone, and the later runs were clean.
- **Step 4 plan signed off, 2026-11-04 ([plan](plans/M3/M3-step4-stderr.md), v2.1).** Step 4
  makes the server's logging non-blocking: a log call never waits for stderr. Lines go into a
  bounded queue that one background thread writes to its own copy of stderr. If the client
  stops reading, lines are dropped and counted, and a `dropped N log line(s)` notice marks the
  gap. `skeptic` found 14 issues (2 medium); `verifier` confirmed 9, rated 1 plausible, refuted
  3 as stated and added 3 of its own, and every confirmed one changed the plan. The main ones:
  - Python's own `QueueListener` writes through a handler whose lock `logging.shutdown` waits
    for, so a stuck stderr would hang the server's exit;
  - `queue.Queue` can deadlock a thread against itself when a finalizer logs on a full queue
    (3 of 3 on Python 3.14);
  - a log call that fails to format writes a traceback to `sys.stderr` under the handler's
    lock, which froze every thread's logging on a stuck pipe.

  Victor approved all four places where the plan changes the contract's wording (Q1–Q4):
  - a plain handler with its own writer thread, a `SimpleQueue` and `os.write`, instead of
    `QueueHandler` and `QueueListener`;
  - every exit waits at most 20 ms for queued lines. This amends S1-D5's "~50 ms": the POSIX
    hand-off's worst case is now about 70 ms, still inside Claude Code's 100 ms;
  - a queued line is capped at 16,384 characters, keeping its first and last 8,192;
  - a log call that fails to format queues a one-line note instead of the stock traceback.
- **Step 4 — non-blocking stderr, done 2026-11-05
  ([plan](plans/M3/M3-step4-stderr.md) §9; commit `de9e10c`).**
  - **Now:** a client that stops reading the server's stderr no longer freezes the server.
    - Log calls only put the line in a queue (1,000 lines, each cut to 16,384 characters). One
      background thread writes the queue to its own copy of stderr.
    - When the queue is full, lines are dropped and counted. `stderr was not being read:
      dropped N log line(s)` marks the gap once lines go through again.
    - Every exit waits at most 20 ms for the queue. The POSIX shutdown line is skipped when the
      queue didn't drain in time or stderr has no room, so the server still exits in Claude
      Code's first 100 ms.
    - Python warnings go through the same queue. `vestibule-mcp setup` and `audit` keep the
      plain handler.
  - **How we know:**
    - **Red first:** the new end-to-end test ran on the old code first. Call 6 never answered,
      with 3,997 bytes sitting unread in a 4 KiB stderr. Now every call answers, and the server
      exits in the SIGINT phase.
    - **Gate:** Linux has 799 tests on Python 3.14, 0 skipped. Windows has 637, plus 142
      Linux-only skips.
    - **Mutants:** 23 deliberately broken versions each turned a test red. Among them are a
      `queue.Queue` that deadlocks when a finalizer logs (Python 3.14), and a writer that
      hangs the exit.
  - **Review:** `critic` ran twice, each round checked by `verifier`. Nothing high or medium.
    - **The main changes:** test 1 now times each dropped call, and the Windows drain must come
      after the warning it exists to save.
    - **The hand-off's line** is now written only when the queue drained in time. A writer
      still blocked on the pipe would otherwise take the room the line needed.
  - **Left for Victor, approved 2026-11-05:**
    - that drain gate, which can also drop the line on a very busy machine (never the exit);
    - an exception whose own text can't be printed is named by its type, instead of counting
      as a dropped line.
  - **Not fixed, recorded:** on Windows, an exception that escapes `server.run` is printed
    straight to stderr, so on a stuck pipe the exit waits for the client's kill. It is older
    than step 4.
- **Review changes to steps 3 and 4 approved, 2026-11-05.** Each review changed something
  Victor had signed, and each change waited for his OK. He committed both steps with the
  changes in, then approved them:
  - **Step 3:** R4 as tightened (output that isn't UTF-8 is pure ASCII), and the warning
    when the audit dir overlaps the workspace or the rootfs.
  - **Step 4:** the shutdown line is written only if the log drained in time, and an
    exception whose own text can't be printed is named by its type.
- **Step 5 plan signed off, 2026-11-06 ([plan](plans/M3/M3-step5-acceptance.md), v2).** Step 5
  is M3's exam, in two parts at Victor's request (S5-D1):
  - **5a:** tests through `server.call_tool` with real backends and a real audit file, and the
    cold-start benchmark run again;
  - **5b:** live Claude Code sessions on Linux and Windows, each ending with a call cancelled
    by Ctrl+C. Then the docs pass, and "M3 accepted" last.

  `skeptic` found 13 issues (6 medium); `verifier` confirmed 11 and rated 2 plausible. Every
  one changed the plan. The main ones:
  - the live check's cancelled call had the 10 s default timeout, so a late Ctrl+C would have
    recorded "timed out";
  - the file-reader test's assertions couldn't fail;
  - an end-to-end `audit` test repeated one step 3's review had dropped.

  Out of that came a rule: 5a adds a test only where real backends or real runs are unproven,
  and maps what is already tested with the real component. S5-D2 (Windows' stderr check: Linux
  only) and S5-D3 (one sentence in golden rule 1 for `audit`'s stdout) were taken as
  recommended with the sign-off. Both belong to 5b.
- **Step 5a — the acceptance suite, done 2026-11-06
  ([plan](plans/M3/M3-step5-acceptance.md) §9; commit `03f0fb7`).**
  - **Now:** M3's criteria are tested through the server's own tool dispatch, with real
    backends and a real audit file (`tests/test_audit_acceptance.py`, 15 tests):
    - every outcome's records on native: ran, timed out, cancelled, refused, not run, blocked;
    - the container's image id, and a missing image refused;
    - real usage numbers for `print(1)`, a memory bomb, a fork bomb, a busy loop and a file
      reader;
    - a guest that can't fake its numbers, on all three backends;
    - native without a systemd bus;
    - the audit folder invisible from inside the sandbox;
    - a log that can't write, while the run still happens.

    What the real components already prove is mapped, not repeated. No `src/` change.
  - **How we know:** 11 deliberately broken versions each turned their own test red. The
    failures were checked: each was the property that broke. Linux has 814 tests, 0 skipped,
    and Windows 641 plus 153 Linux-only skips.
  - **The benchmark:** native's median is now 118 ms and the container's 895 ms, against the
    README's 82 and 392. The code from before M3 measures the same today, so M3 costs nothing
    measurable: the host got slower. 5b updates the README's numbers.
  - **Review:** `critic` found 9 issues and `verifier` confirmed or partly confirmed 7; none
    was left high or medium. The fixes:
    - teardown drains every warden used;
    - the leftover-scope check can't be skipped by accident;
    - the cancel waits until the guest runs.
  - **Found, not fixed:** a cancel that lands during native placement, a window of about 15 ms,
    can leave an empty systemd scope for up to timeout + 30 s. It is older than M3. The fix
    waits for Victor (BUILD_LOG's known issues).
- **The placement-cancel fix's plan signed off, 2026-11-06
  ([plan](plans/M3/M3-step5-placement-cancel.md), v2).** Victor chose to fix 5a's finding before
  5b.
  - **The cause, now measured:** a cancel during native placement makes the paused helper exit
    at once. systemd can then attach it while it is a zombie (exited, not yet reaped), and the
    scope is born empty and stays active until its `RuntimeMaxSec`. The same can happen when
    placement finds the helper gone.
  - **The fix:** the run carries a flag while placement is unfinished. Its finisher then stops
    `vestibule-<id>.scope` by name, within 5 s, after the helper is gone.
  - **The review:** `skeptic` found 7 issues (1 high); `verifier` confirmed or partly
    confirmed 7 and found 1 plausible, and its experiments found the cause. The high one: v1's
    test couldn't move a process into the scope from where the gate runs pytest. v2's tests
    hand systemd a zombie's pid instead, which makes the real empty scope on demand.
  - **The decision this amends:** M2's S4-D6 ("the finisher does nothing new").
- **The placement-cancel fix, done 2026-11-06
  ([plan](plans/M3/M3-step5-placement-cancel.md) §7; commit `7a3c30f`).**
  - **Now:** a call cancelled while native is placing its run (Ctrl+C in Claude Code) no longer
    leaves an empty systemd scope. The same holds for a placement that finds the helper gone.
    The run's cleanup stops the scope by name once the helper is gone, within 5 s. A normal run
    never sends that stop.
  - **How we know:**
    - two tests reproduce the real empty scope by handing systemd a zombie's pid. They failed
      on the old code and pass now;
    - cancels at random moments left a scope 2 times in 40 before the fix, and 0 in 100 after;
    - 9 deliberately broken versions each turned their own test red;
    - Linux has 818 tests, 0 skipped, and Windows 641 plus 157 Linux-only skips.
  - **Review:** `critic` ran twice on the code and found only low issues. One was a broken
    version that every test missed: it would have held a run's slot 5 s longer on a hung bus.
    A test now catches it.
  - **Still not covered, stated:**
    - any POSIX shutdown during placement, where the cleanup never runs;
    - a placement whose 5 s bound passes after systemd answered.

    Both are rare, and the scope still goes at its time limit.
- **Step 5b — the live checks and the docs, done 2026-11-07
  ([plan](plans/M3/M3-step5-acceptance.md) §9; commit `c615671`).**
  - **Linux Claude Code, live.** Victor ran a session with only Vestibule's two tools. Five
    native runs; the first four results each said `isolation: native`, and the fifth was
    cancelled:
    - `print(1)`'s result read `cpu=36ms, mem_peak=13MiB, pids_peak=2`;
    - a busy loop with a 2 s timeout was stopped, and showed it was throttled in 40 of 41 CPU
      periods;
    - a memory bomb died at its 256 MiB limit with the note saying so;
    - a node hello;
    - a 60 s sleep, cancelled with Ctrl+C.

    `vestibule-mcp audit` then listed exactly those five, in order, the cancel included. The
    folder was readable only by Victor, the server shut down cleanly, and nothing was left.
  - **Windows Claude Code, live.** Four calls in containers: two hellos, the busy loop and the
    cancelled sleep. Each result carried its wall time and said why the other numbers aren't
    measured in a container. `vestibule-mcp audit` showed all four, typed plainly and piped
    alike, and no container was left.
  - **Seen on the way,** none of it in Vestibule's code:
    - Claude Code opens each session with a newer kind of request, as it did at M1's live check
      (above). The MCP library answers it with an error and logs a long warning, then the
      session connects normally;
    - after the cancel, Claude Code again reported Vestibule's connection as dropped, as it did
      in M2;
    - on Linux, Claude Code's safety classifier held back some of the agent's chat text, but
      every call ran as written.

    The first is now a BUILD_LOG known issue as well, and both are in GETTING_STARTED's
    troubleshooting.
  - **Docs:** the README explains the usage numbers and the audit log (what a record keeps, who
    can read it, that a failed write never stops a run, that it isn't tamper-proof, and that
    old files are deleted by hand), and its speed table has this week's numbers. `CLAUDE.md`,
    `ARCHITECTURE.md`, `PLAN.md`, `GETTING_STARTED.md` and the contract are brought up to date,
    and the contract has its acceptance record, criterion by criterion.
  - **Golden rule 1 gets one sentence (S5-D3, approved with the plan):** the terminal commands
    aren't the server, so `vestibule-mcp audit` printing its table to stdout is allowed. The
    rule itself is unchanged.
  - **Review:** `critic` found 9 issues in the docs, 2 medium; `verifier` confirmed all 9 and
    lowered one medium to low. The medium one left: the docs called Claude Code's
    `server/discover` warning new, though M1's live check had already recorded it. The other
    medium: one README sentence said M3 had passed before the review was done. The small ones
    were wording that claimed too much: call 5 had no `isolation:` result, a run that never
    started shows its wall time alone, a config error leaves no record, an existing audit
    folder keeps its own permissions, and the event-loop lines read as complete lists. All
    fixed.
  - **Totals:** 819 tests on Linux with none skipped, 641 plus 158 Linux-only skips on Windows;
    ruff and mypy clean.
- **M3 accepted — 2026-11-07.** Every one of the 10 criteria has its evidence: the exam through
  the server (5a), the gates on both platforms, and the live sessions with Claude Code on Linux
  (native) and Windows (container), each ending with a cancelled call that the audit log
  recorded. The criterion-by-criterion record is in the contract. What carries forward:
  - **M4:** `SECURITY.md` (the audit isn't tamper-proof; the audit folder's permissions on
    Windows; the stderr writers left on crash paths); the leftover `%TEMP%` dirs after a hard
    kill.
  - **Upstream, with the `mcp<2` move:** the SDK's answer to a cancelled call, and its warning
    for `server/discover`.
  - **Stated residuals:** a POSIX shutdown during native placement, and a placement whose 5 s
    bound passes after systemd answered. What stderr Claude Code on Windows hands the server
    stays not determined (S5-D2).
  - **Not in M3, by decision:** workspace quotas (they need root) and retention (old files are
    deleted by hand).

## M4 — Polish for adoption (in progress)

Goal: a stranger installs Vestibule and gets a sandboxed result in under 2 minutes on a clean
machine, following only the README; and `SECURITY.md` says honestly what it protects and what
it doesn't. Contract: [`docs/plans/M4/M4-adoption.md`](plans/M4/M4-adoption.md) (signed off
2026-11-07).

### Decision sign-offs

- **The M4 contract — signed off 2026-11-07 (v2).** `skeptic` reviewed v1 (19 findings, 3
  high) and `verifier` checked its 15 checkable claims, confirming most and finding two more
  problems. v2 takes every finding (the contract's §8). The two that changed the plan most:
  - an sdist built from the working tree would have shipped local files such as
    `.claude/settings.local.json`, so the release builds from a clean export with an include
    list;
  - a demo with only Vestibule's tools would have implied more than the README installs, so the
    README now says the boundary is code sent to `run_code` only, and shows the sandbox-only
    session.

  Victor chose the recommended option on all nine decisions:
  - **P1 — install with `uv tool install vestibule-mcp`.** The command every message names is
    then on `PATH`, `setup` and the server are one version, and uv brings its own Python.
    Declined: `uvx` (the named fixes wouldn't run, and versions could differ), and `pipx` (it
    needs Python and pipx first).
  - **P2 — release on mcp 1.x, narrowed to the minor the gates ran;** the 2.x move gets its own
    milestone after M4. This **amends M2's S1-D1** ("before the PyPI release"). Declined:
    rewriting the protocol layer first.
  - **P3 — keep M2's Ubuntu decision (N5).** Stock Ubuntu 23.10+ gets the container backend,
    with Docker Engine from Docker's apt repo. The snap can't run Vestibule. No AppArmor
    profile; the `docker` group's cost is stated.
  - **P4 — one `setup` command everywhere,** downloading only what the machine uses.
  - **P5 — `setup` ends with the real readiness check,** worded for the terminal's environment.
  - **P6 — Victor runs the first-run test, following only the README,** on fresh VMs at
    25 Mbit/s, against the release candidate before the upload. It includes live Cursor and
    Claude Desktop sessions.
  - **P7 — a real recorded demo GIF,** with a caption saying what it shows.
  - **P8 — GitHub's private vulnerability reporting** (Victor turns it on).
  - **P9 — one manually triggered macOS job** for the Docker-free tests and the smoke test.
  - **Out of M4:** the mcp 2.x move, the Windows→WSL bridge, a custom image, the liveness
    lease, container runs on macOS, arm64, standing CI.

### Build steps (6 planned: 0–5)

- **Step 0 — the spike, no repo code: parts A and B done 2026-11-08
  ([report](plans/M4/M4-step0-spike.md)).**
  - **Part A, on this machine:**
    - `uv tool install` from a clean wheel works on Windows (3.11, and a uv-managed 3.14) and
      on Linux (3.11–3.14), and on Ubuntu 24.04's and Debian 13's own Pythons.
    - Each first call reports the expected isolation: `container` on Windows, `native` on
      WSL.
    - Uninstall deletes don't follow symlinks on either platform.
  - **Three things the README must say:**
    - Windows needs `uv tool update-shell`.
    - Linux's uninstall needs `chmod -R u+rwX` first, because a guest can lock folders in its
      workspace.
    - A guest's links can point outside the workspace on the host: real NTFS symlinks on
      Windows with Developer Mode on.
  - **Part B, on the VMs and the GUI clients:**
    - Claude Desktop and Cursor both find `vestibule-mcp` and `docker` by bare name after
      `uv tool update-shell`, with no re-login.
    - Docker Desktop runs inside a nested Hyper-V VM: `container`.
    - Stock Ubuntu 24.04 is refused with the right hint. Snap Docker fails on
      `no-new-privileges`. Docker from its apt repo gives `container`.
    - Debian 13 minimal has no user bus: `native-degraded`, then `native` after
      `dbus-user-session` and a reboot.
  - **What part B adds for the README:**
    - A minimal Debian needs `ca-certificates` before `setup` works.
    - Debian's user-bus fix needs a reboot.
    - Docker's recommended Windows install needs Virtual Machine Platform turned on first.
    - uv may pick Python 3.15 when there's no usable system Python.
  - **Victor signed off the contract amendments on 2026-11-08,** and they're applied. Victor
    also chose two small messages for step 2: `setup` names `ca-certificates` when a download
    fails certificate checks, and the error under snap Docker names the snap and points to
    Docker's apt repo. Step 0 is done.
- **Step 1's plan signed off 2026-11-08** ([plan](plans/M4/M4-step1-run-dir-sweep.md)): the
  sweep of run dirs a hard kill leaves. Victor kept F3's layout over `skeptic`'s
  `~/.vestibule/runs` alternative, and took T1–T3 as written: undated dirs are skipped
  silently, pre-M4 leftovers stay, and a host on full native never sweeps.
- **Step 1 done 2026-11-08** ([plan](plans/M4/M4-step1-run-dir-sweep.md)), commit `7e977fb`.
  - **What changed:** a container run's script folder now has its deadline in its name, and
    on Linux and macOS its parent folder has the server's pid. Each reaper pass first removes
    folders more than 60 s past their deadline, and empty parents whose server is gone. So a
    hard-killed server no longer leaves its folders behind for good.
  - **How it was checked:** a real hard kill followed by a second server, a real root-owned
    folder, and 25 of 25 mutants red. The gate is green on both platforms.
  - **What stays:** folders from before this step are never removed, a host on full native
    never sweeps, and one narrow Linux case can leave two empty root-owned folders until
    reboot (the plan's §5).
- **Step 2's plan signed off 2026-11-08** ([plan](plans/M4/M4-step2-setup.md)): one `setup` on
  every platform that ends in the real readiness check. Victor took T1–T4: an image the machine
  needs that couldn't be pulled exits 1 even when the check says `container`; only missing images
  are pulled; `setup` prints its own report, not the server's log lines; a `docker pull` has no
  time limit once started. Victor also agreed to one live run on the Ubuntu VM after the gate.
- **Step 2 done 2026-11-09** ([plan](plans/M4/M4-step2-setup.md)), commit `ff10e3c`.
  - **What changed:** `vestibule-mcp setup` is now one command on every platform. It downloads
    only what the machine can use: no native trees where user namespaces are refused or for a
    root user, no images where native is full. It ends with the server's real selection and
    prints `ready in this terminal's environment: isolation X`, or the exact `Blocked:` fix,
    plus what can differ for a client started another way.
  - **Messages:** a certificate failure names `ca-certificates`, a probe failure under snap
    Docker names the snap and Docker's apt repo, and a host that refuses user namespaces is
    no longer sent back to `setup`.
  - **How it was checked:** 53 of 53 mutants caught, the gate is green on both platforms,
    and a live run on the stock Ubuntu VM showed the real AppArmor refusal, the pulls, and a
    first tool call matching `setup`'s answer.
  - **What stays:** snap Docker still downloads the two images before its real probe fails,
    and the README keeps its two `docker pull` lines until step 4.
- **Step 3's plan signed off 2026-11-09** ([plan](plans/M4/M4-step3-release.md)): the release
  packaging. Victor took U1–U5: only the packages users install move up, the dev tools stay; this
  session's `vestibule` MCP server is stopped once for the Windows venv rebuild; Victor runs the
  secret scan of the history at release time, because it hasn't been scanned yet; the macOS job runs
  after the usual pull request; and on macOS the stdio end-to-end check is the smoke test's stdio run.
- **Step 3 done 2026-11-09** ([plan](plans/M4/M4-step3-release.md)), commit `cbf6424`. Nothing was
  published.
  - **What changed:** version 0.1.0 and its metadata. The sdist is 27 files, not 113. The
    dependency set is what a fresh install gets today, `mcp` 1.30.0 included, and the published
    range is that minor. Both images are re-pinned. New: the release checklist
    ([`RELEASE.md`](RELEASE.md)), `scripts/release_smoke.py`, and a manual macOS job.
  - **How it was checked:** the real smoke passed on Python 3.11 through 3.15, on Windows and in
    WSL. Each stage was shown to fail on a bad wheel or a bad state. 41 of 41 mutants caught, and
    the gate is green on both platforms.
  - **What stays:** the history hasn't been scanned for secrets (Victor runs it from the
    checklist), and the macOS job hasn't run: it needs the workflow on `main`.
- **Step 4's plan signed off 2026-11-09** ([plan](plans/M4/M4-step4-docs.md)): the README,
  `SECURITY.md` and the demo GIF. Victor took option (1) on V1–V4. The live client sessions and a dry
  run on the two Linux VMs happen in this step, before the README is frozen. The headline is reworded,
  because an agent on a hosted model does send its code and output to that model's provider. The GIF
  is made with `asciinema` and `agg`. And `read_workspace`'s check-then-open race, which M1 accepted
  as a residual, is fixed before the release, as a small step of its own: the review reproduced it
  with a swap made from the host, and the tool returned a file from outside the workspace.
- **The `read_workspace` race fix's plan signed off 2026-11-09**
  ([plan](plans/M4/M4-step4-workspace-race.md)): V4's small step. Victor took option (1) on X1–X3.
  The tool will open each part of a path once, from the folder it already holds open, and never
  follow a link, on every platform. On Windows that is the same walk as on Linux, through the
  native `NtOpenFile` call, which changes the Windows half of V4's sentence (a check of the opened
  handle's path): that check stopped the leak but left a racing guest able to learn whether a host
  path exists. The old by-name check is removed, because the review showed it gave that away on
  Linux too. The FIFO hang goes in with it, and Windows lists a folder through its handle.
- **The `read_workspace` race fix — done 2026-11-09** (commit `364f975`;
  [plan](plans/M4/M4-step4-workspace-race.md), §10 as built).
  - **What it does now:** the tool opens each part of a path once, from the folder it already
    holds open, and never follows a link. A guest that swaps a folder for a link while the tool
    runs gets a refusal, or the real folder.
  - **How bad it was:** with a real sandboxed guest doing the swap in a loop, 8,003 of 226,507
    reads on Linux returned a file from outside the workspace, and 5 of 23,513 on Windows. After
    the fix: none in 589,063 and none in 51,562.
  - **Three more things the same change closed:** a guest could hang a call for good with a
    FIFO, stall it 45 s with a file lease, and learn whether a file exists on the host.
  - **One bug found on the way, and fixed here by Victor's choice:** a file whose name isn't
    valid text stopped the whole server at the next directory listing. It now shows with a
    replacement character.
  - **What it leaves, stated:** a Windows server whose workspace is on a network or WSL share
    still walks by name, and a racing guest can hold one call for about 10 s there. A file name
    can make a listing look like it has more entries on a Linux host, and a listing isn't cut to
    the output cap. These are in BUILD_LOG's known issues.
  - **How it was checked:** every test was red on the old code first. 34 of 35 mutants were
    caught, and the last can't fail on a default Windows. `critic` reviewed the diff twice, and
    the gate is green on both platforms. macOS hasn't run: Victor runs the manual job on the
    branch after the push.
- **Step 4a — done 2026-11-09** (commit `5a1bc23`; [plan](plans/M4/M4-step4-docs.md), §13 as
  built): the README and `SECURITY.md`.
  - **The README is written for a stranger now.** It says what is and isn't sandboxed (only code
    sent to `run_code`), gives a quick start per platform that starts from `uv tool install
    vestibule-mcp`, shows the config for each client with what tested it, and ends with how to
    uninstall. Its headline no longer says nothing is sent to a cloud service: an agent on a
    hosted model does send its code and output to that model's provider.
  - **`SECURITY.md` exists.** It says what is out of a run's reach when the result says `native`
    or `container`, what stays possible, how each verdict is reached, and 14 things it leaves
    open, each with where the claim comes from.
  - **Victor tested it by hand:** Claude Code on Windows and in WSL with the short name, Cursor
    and Claude Desktop on Windows, and the quick start's lines up to `setup` on a fresh Debian VM
    and a fresh Ubuntu VM, with a local build standing in for the PyPI name. All passed.
  - **The macOS job ran for the first time and passed** (849 passed, 29 skipped). No sandboxed
    run has been made on a Mac, and both documents say so.
  - **Private vulnerability reporting is on,** so the link in `SECURITY.md` works.
  - **What the sessions corrected:** the "sandbox-only" line removes Claude Code's own tools and
    nothing else. Other MCP servers stay until a stricter line is used, and plugins' hooks run
    either way. The README now says all three.
  - **How it was checked:** three review rounds on the two documents, each finding sentences
    that claimed a little more than the code does, twice in a sentence the previous round's fix
    had added. All confirmed findings are fixed. The doc tests were written by the new `builder`
    agent (Opus 5.5) and shown to fail 39 ways. The gate is green on both platforms.
  - **What's left of step 4:** 4b, the demo GIF.
- **Step 4b — done 2026-11-10** (commit `bc79e75`; [plan](plans/M4/M4-step4-docs.md), §14 as
  built): the demo GIF.
  - **What it shows, in about 20 seconds:** a fake key file that is readable on the machine,
    then an agent asked to read it and to send something out. Both fail inside the sandbox,
    with `No such file or directory` and `Network is unreachable`, and both results say
    `isolation: native`.
  - **It's a real session,** recorded by Victor in WSL, where `run_code` was the agent's only
    way to run code. Every edit is in time, and the caption lists them: the start-up banner,
    the pasting of the prompt and the first 2.4 s of the agent's turn are cut, pauses are
    capped at 2 s, it plays 1.3 times faster, it ends at the second result, and the last frame
    is held.
  - **It took six takes.**
    - In the first three the prompt was given on the command line and went out before the
      server had connected, so the agent had no tools.
    - Fable 5.2's safeguard also stopped the first two. The demo names Opus 5.5; Fable 5.2
      wasn't tried again once the agent had its tools.
    - In the next two the model made both calls without showing the first result, so the demo
      asks for the two calls in two messages.
  - **How it was checked:** what the agent typed was compared with the tool's own results and
    matches exactly. Every screen was checked for an email or account details; the only name on
    screen is the Linux user name in the paths, which is the GitHub name. A new test keeps the
    file a GIF under 3 MB. `critic` reviewed the caption and the records, and its findings are
    fixed.
  - **What it leaves for step 5:** whether the GIF shows on the PyPI page. It can't until the
    branch is on `main`.
- **Step 4 follow-ups — plan signed 2026-11-10**
  ([plan](plans/M4/M4-step4-followups.md), v2 after the `skeptic` review). Victor asked for the
  open issues to be fixed before step 5 is planned, and took the recommendation on both
  decisions.
  - **K1:** a directory listing reads at most 10,000 entries. Today it reads every one, and a
    folder of a million files takes 7 seconds to list.
  - **K2:** the new message for a Linux user who can't open Docker's socket names the `docker`
    group, and says that its members can become root on the machine.
  - **Also in the plan:** a file name can no longer forge extra lines in a listing, and a
    listing is cut to the output cap.
- **Step 4 follow-ups — done 2026-11-10** (commit `4bd503c`;
  [plan](plans/M4/M4-step4-followups.md), §13 as built): the open issues in directory listings
  and in `setup`'s Docker message.
  - **A file name can't fake a listing any more.** A sandboxed run could name a file so that
    `read_workspace` showed the agent an entry that wasn't there. Names are now shown with
    their control characters escaped, one entry per line.
  - **A listing is bounded.** It stops at the output cap, and it reads at most 10,000 entries
    of a folder. A folder of a million files took 7 seconds to list; 20,000 files now take a
    hundredth of a second, and a tenth with the workspace on a slow disk.
  - **A Linux user who can't open Docker's socket is told why:** join the `docker` group, log
    out and in, restart the agent, and what that group means. Before, the message said to
    start Docker, which was already running.
  - **What the reviews changed:**
    - The plan review found that asking each file's size was the slow part on a slow disk, so
      sizes are asked only for the 200 entries shown.
    - The code review found that a run deleting its files during a listing could still force
      10,000 lookups, and that a folder made unreadable listed as empty. Both are fixed, and
      such entries now show as `unknown`.
  - **How it was checked:** every new test failed on the old code first, the mutants were
    caught, the gate is green on both platforms, and a real sandboxed run made the hostile
    names and the big folder on Linux and on Windows.
  - **Two small docs fixes went in with it,** with Victor's OK: `CLAUDE.md` no longer says
    "structurally impossible", and the M2 step 2 line above names its review-fix commit.
  - **What it leaves:** a folder past 10,000 entries is listed from the first 10,000 read, and
    names that aren't valid text can look alike. Both are in `SECURITY.md`.
- **Step 5 — plan signed 2026-11-10** ([plan](plans/M4/M4-step5-release.md), v2 after the
  `skeptic` review): the first runs on fresh machines, the release, and "M4 accepted". Victor
  took the recommendation on all five decisions. The sign-off starts the part that publishes
  nothing; the upload waits for his separate go.
  - **S5-D1:** the release candidate is built once, from the branch, and the two files the
    test machines run are the two files uploaded. `main` gets the commit when the tests pass.
  - **S5-D2:** Claude does a timed dry pass on each machine first, and restores it, so that
    Victor's run is still a first run.
  - **S5-D3:** the README's last quick-start sentence names the tool: ask your agent to run
    `print(1)` with Vestibule's `run_code` tool. Without that, an agent may run it with its own
    shell, on the machine.
  - **S5-D4:** the published README's M4 row says "first release, 0.1.0".
  - **S5-D5:** after the upload, one machine (Debian) confirms the first run from PyPI.
  - **What the review changed:** the clock stops at the first finished run, not the first
    record; the 25 Mbit/s limit is measured before anything is timed; and the uninstall test
    gets a locked folder and a before-and-after listing, so that it can fail.
- **Step 5a — the first runs on fresh machines, 2026-11-11 (all tests run; candidate `4bd503c`;
  nothing published).**
  - **Before the runs:** the gate, the build, the smoke test on five Pythons per platform, the
    secrets scan and the macOS job all passed on the candidate.
  - **All four runs ended in the right isolation,** with only the README's commands typed:
    Debian `native`, Ubuntu without Docker refused with the reason and the fix, Ubuntu with
    Docker `container`, Windows `container`.
  - **The two-minute target:** Ubuntu with Docker took 117.7 s. Debian took 150.5 s, of which
    about 84 s is downloading and the model, on a line held to 23.6 Mbit/s. Windows wasn't timed
    cleanly.
  - **Decided by Victor, 2026-11-11:** the criterion will say what was measured, and 0.1.0's
    `setup` download isn't cut. The Windows VM runs on an uncapped line, because nothing on
    the host could limit a VM's downloads; its time at 25 Mbit/s is an estimate.
  - **How the line was held on Linux:** Hyper-V's cap limits only what a VM sends, so the two
    Linux VMs shape their own downloads (measured at 23.6 Mbit/s).
  - **Uninstall passed on Debian and on the Windows VM.** On each, the agent first locked
    something in the workspace and made a link to a read-only folder outside it; then Victor
    typed the README's uninstall lines. Everything of Vestibule's was gone and the outside
    folder was untouched.
    - On Windows the guest's own link was one Windows doesn't follow, so the link that was
      tested was a real one made by the test kit.
    - Two things stay that the README's "What stays" list doesn't name, neither of them
      Vestibule's: uv's package cache and Claude Code's log folder for the server.
      **Victor, 2026-11-11: 0.1.0's README stays as built;** the wording is fixed in the next
      release.
  - **Cursor passed on the candidate** on the dev PC: `setup` said `container`, Cursor's
    first call ran in `container`, and the audit log names Cursor.
  - Details are in [`BUILD_LOG.md`](BUILD_LOG.md).
- **Step 5b — the release of 0.1.0, done 2026-11-11 on Victor's go. `vestibule-mcp` 0.1.0 is
  on PyPI: Victor uploaded the two tested files at 22:14 UTC, and the smoke test from PyPI
  passed on five Pythons on Windows and on Linux.**
  - `main` already held the candidate (pull request #31).
  - **The checks before the upload all passed:** `main` ships the candidate's files, the two
    files have their recorded hashes, the smoke test passed again on five Pythons per
    platform, every link in the README works, and the name is free on PyPI.
  - **The first run from PyPI, on Debian, ended in `native`** with only the README's lines
    typed. Its clock doesn't count: Claude Code's login in the VM had expired and Victor
    logged in again inside the run.
- **Step 5c — 0.1.1, a shorter page on PyPI: plan signed 2026-11-11**
  ([plan](plans/M4/M4-step5c-pypi-page.md), v2 after the `skeptic` review).
  - **Why:** Victor looked at 0.1.0's page on PyPI. He doesn't want it to carry the parts
    about how the project was built, or the early-development warning. GitHub's README stays.
    A published description can't be edited, so the page changes with a new release.
  - **Decided by Victor:**
    - the PyPI page leaves out the `Status` and `Documentation` sections, the warning and
      the "early development" badge;
    - `How it works (design)` stays on the page. He first chose to drop it, on a question
      that described the section wrongly; it is where the page says what the `isolation:`
      values mean and where the promise ends;
    - PyPI's "Alpha" label stays;
    - the README's "What stays" list gets its two missing lines now;
    - 0.1.1 goes up before "M4 accepted" is written.
  - **How:** a small script cuts `PYPI.md` from `README.md`, and the package names `PYPI.md`
    as its description. A test fails if the two drift apart. The package's code doesn't
    change.
  - **What the review changed:** the first plan put marks into the README, and a mark beside
    the badge would have broken GitHub's row of badges. The script now cuts named parts and
    the README isn't edited for it.
- **Step 5c — 0.1.1 built and released, 2026-11-11** (commit `02fc3b8`).
  - **Built as planned:** the script, `PYPI.md`, version 0.1.1. Against 0.1.0 the package's
    code differs in one line, the version number.
  - **Checked before the upload:** both gates, the smoke test on five Pythons per platform,
    the secrets scan, the macOS job, every link on the new page, and the page's outline as
    PyPI's own renderer draws it.
  - **Victor uploaded it at 23:50 UTC.** PyPI holds the two tested files, and its page text is
    `PYPI.md`. The smoke test from PyPI passed on both platforms.
    - Right after the upload an installer still got the old index page and couldn't find
      0.1.1: a try 98 s after it failed, and one at 134 s passed, with nothing changed.
  - **Not run again:** the first runs on the VMs, the uninstall tests and Cursor. No command a
    first run types changed, so 0.1.0's results stand, the missed two minutes on Debian
    included.
- **After 0.1.1, 2026-11-11: the README's quick start is reordered, on GitHub** (Victor's
  ask): Windows, Ubuntu, Linux with systemd, macOS. Ubuntu now comes before the general Linux
  section, because Ubuntu is where that section's steps end in `Blocked:`. No command
  changed. PyPI's page gets the new order with the next release.
