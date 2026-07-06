# M1 step 5 plan — backend selection & capability probing

Status: **signed off & built 2026-07-05** (S5-D1/D2/D3 approved as recommended; see
`docs/HISTORY.md`).
Parent contract: `docs/plans/M1-container-backend.md` §5 (binding; S5-D1/S5-D3 amend it slightly).
Review history: Codex adversarial review 2026-07-05 — two high findings. (1) The step-4
timeout path could still eat the whole outer deadline; fixed in code the same day (see the
step-4 plan's 2026-07-05 amendment). (2) The original S5-D3 deferred image preflight to
step 6, leaving a first node run able to trigger a silent `docker run` auto-pull inside a
tool call — a D2 violation. S5-D3 below is the **revised** version: preflight +
`--pull=never` land in this step. A final pre-build logic check then caught a third gap:
this plan's edge-case list claimed step 4's S4-D2 covers "daemon dies after a good probe" —
it does not (S4-D2 only fires when the docker *binary* is missing; a dead daemon makes
`docker run` exit **125** with the container never started). As built: exit 125 reports
`isolation: none` + "nothing was executed", and the server drops the cached selection so the
next call re-probes. A guest deliberately calling `exit(125)` is indistinguishable at the
CLI level and gets underclaimed isolation plus one cheap re-probe — the safe direction.

---

## 1. What this step does, in plain words

Today the server still uses the M0 stub: `get_warden()` always returns the **unsafe naive
backend**. The locked-down container backend from steps 3–4 exists, but nothing selects it.

Step 5 wires in the real chooser. On the **first tool call** (never during MCP startup),
Vestibule checks what this machine can actually do — is Docker there? is the image pulled?
does the full locked-down profile really work? — and then commits to one of three honest
answers:

| Verdict | Meaning |
|---|---|
| `container` | The full profile was **test-driven and passed**. Runs report `isolation: container`. |
| `container-degraded` | Isolation works, but some resource limits don't. Runs proceed and say exactly which limits are off. |
| **Blocked** | Real isolation is impossible here. Every `run_code` returns a `Blocked:` message with the exact fix (e.g. "start Docker Desktop"). **Never** a silent fallback to no isolation. |

The naive backend becomes reachable only by explicitly setting `VESTIBULE_BACKEND=naive`.

## 2. The checklist (runs once, result cached)

1. **Dev override?** `VESTIBULE_BACKEND=naive` → use NaiveBackend (`isolation: none`). Stop.
2. **Find a runtime.** `VESTIBULE_RUNTIME=docker|podman` → try only that one;
   `auto` → Docker first, then Podman. Check = `<runtime> version` (5 s cap).
   None respond → **Blocked**: "no container runtime — install/start Docker Desktop, then retry."
3. **Is the probe image present?** `<runtime> image inspect python:3.12-slim` (5 s cap).
   Missing → **Blocked**: "run `docker pull python:3.12-slim`, then retry." (No auto-pull — D2.)
4. **Test-drive the full profile.** Run a tiny real script through `ContainerBackend.run()` —
   the *same code path real runs use*, so the probe can never drift from reality.
   Passes → verdict **`container`**. Done.
5. **One retry without the soft limits** (memory / cpu / pids / tmpfs-size — see S5-D2).
   Passes → verdict **`container-degraded`**; all later runs omit those flags and say so.
   Fails → **Blocked**: "runtime cannot enforce the isolation profile: `<last error>`."

**Found by the probe (build-time):** step 3's script delivery wrote guest files in text
mode, which on Windows hosts turns `\n` into `\r\n` — and CRLF breaks bash keywords in the
Linux guest (`then\r` is not `then`). Python/Node tolerate CRLF and single-line bash never
hit it, so the RO probe (the first multi-line `if/then` bash guest) exposed it. Fixed:
scripts are always written with `newline="\n"`.

**The probe script** (bash — runs in `python:3.12-slim`, which ships bash):
- Normal mode: write a file in `/workspace`, read it back, delete it, print a marker.
- Read-only mode (`VESTIBULE_WORKSPACE_RO=1`): confirm reading works **and writing fails**.
- Pass = exit 0 + marker in stdout. Probe timeout 10 s (module constant, no new config).

Besides this one-time checklist, **every run** first checks that its own image is present
(S5-D3): a cheap cached `image inspect`, and `--pull=never` in the run profile as backstop —
a missing image is a legible refusal with the pull command, never a silent in-call download.

The whole selection happens *before* the server's per-run outer deadline starts, so a slow
first probe (cold Docker Desktop VM) can't eat a run's time budget. Concurrent first calls
share one probe via an `asyncio.Lock`.

**Budget:** the preflight adds one bounded 5 s CLI call to the run path (worst case, wedged
daemon, first use of an image). Worst honest path becomes: slot wait 5 + preflight 5 +
collect `timeout+5` + CLI wait 5 = `timeout+20` — exactly today's outer deadline. So the
server's outer deadline moves from `timeout+20` to **`timeout+30`**, restoring real margin
(amends contract §7; the outer deadline is a hang backstop, not a UX promise — honest
results still return as fast as before).

## 3. Decisions to sign off

### S5-D1 — Failed checks retry after a 30 s cooldown (amends "cached once per process")
The contract said a failed selection is cached forever. But the #1 real failure is just
"Docker Desktop isn't running" — the user starts it and tries again. With forever-caching
they'd have to restart the whole MCP session. Instead: **success is cached for the process
lifetime; failure is cached only 30 s**, then the next call re-checks. The Blocked message
says so ("…then retry"). Bonus: a probe that fails only because the Docker VM was cold
self-heals on the retry.

### S5-D2 — Degraded mode drops *all* soft limits at once (the contract baseline)
When the full profile fails but hard isolation works, we retry once with all four soft
limits off, and degraded runs then honestly report
`isolation: container-degraded (limits not applied: memory, cpu, pids, tmpfs-size)`.
- **Recommended: keep it this simple.** Degraded environments are rare, the report matches
  exactly what is applied, and selection stays bounded at 2 probe runs.
- *Rejected (for now): per-limit add-back* — re-probing each limit to keep the ones that
  work. Finer-grained, but up to 4 extra probe containers on the first call (could exceed a
  client's tool-call timeout). Revisit only if real degraded environments show up.

### S5-D3 (revised after the 2026-07-05 Codex review) — Per-run image preflight + `--pull=never` land in *this* step
The original version deferred image preflight to step 6. Codex flagged the hole: `docker run`
**auto-pulls a missing image by default**, so a first node run with no local image would start
a multi-minute network pull *inside the tool call* — exactly what D2 forbids. Closed here, not
in step 6:

- **Every run preflights its own image**: `<runtime> image inspect <image>` (5 s cap) before
  anything is spawned. Present → cached for the process (one check per image, ever).
  Missing → refuse with the exact fix: "image `node:22-slim` is not present locally; run
  `docker pull node:22-slim`, then retry. Vestibule never pulls images itself."
- **`--pull=never` goes into the run profile** as the backstop, so even a race (image deleted
  between check and run) fails fast instead of pulling. If that happens, the cached preflight
  for the image is dropped, so the next call re-checks and returns the pull message.
- Selection still only requires the **python** image: the probe is a normal run, so it uses
  the same preflight; a missing node image inconveniences only node runs, never selection.
- Floor: `--pull` needs Docker CLI ≥ 20.10 (Dec 2020) — any modern Docker/Podman. On an older
  CLI the probe fails loudly ("unknown flag") and the run is Blocked; documented in step 7.
- Step 6 shrinks to digest pinning + setup-UX message polish.

## 4. Code changes

| File | Change |
|---|---|
| `src/vestibule/backends/select.py` | **New.** `select_backend(limits)` — the §2 checklist; cached verdict + 30 s failure cooldown; probe helper. |
| `src/vestibule/backends/container.py` | Constructor gains `soft_disabled` (frozenset). `_build_command` skips those flags (tmpfs stays, only its `size=` cap drops) and adds `--pull never`. Per-run image preflight with per-image positive cache (S5-D3). Exit 125 → `isolation: none` ("nothing was executed"). Results report `container-degraded` + detail when any soft limits are off. |
| `src/vestibule/server.py` | `get_warden()` → async, returns the cached selection; selection failure raises `RunRefusedError` (built in step 4 for exactly this) → existing `Blocked:` path. Called *outside* the outer deadline. Outer deadline `timeout+20` → `timeout+30` (§2 budget). `note_result` hook: a container-tier run reporting `isolation: none` drops the cached selection (fresh backend + empty image cache on the next call). |
| `tests/test_select.py` | **New** — Docker-free suite (§6). |
| `tests/test_container.py` | +2 Docker-marked selection tests. |
| `docs/plans/M1-container-backend.md` | §5 annotated as amended by this doc. |
| `docs/HISTORY.md` | Sign-off + build bullets. |

No new config knobs. New module constants: probe timeout 10 s, failure cooldown 30 s.

## 5. What can go wrong (and what happens)

- **Daemon down** → Blocked "start Docker Desktop"; re-checked after 30 s (S5-D1).
- **Docker dies *after* a good probe** → two flavors, both honest (final logic check):
  binary gone → S4-D2 path (`isolation: none`, "runtime unavailable"); daemon gone with the
  binary still present → `docker run` exits **125** with the container never started →
  `isolation: none`, "nothing was executed". Either way the server drops the cached
  selection, so the next call re-probes and returns the actionable message instead of
  failing forever. (A guest deliberately exiting 125 gets underclaimed isolation + one
  cheap re-probe — underclaiming is the safe side of golden rule 5.)
- **`VESTIBULE_RUNTIME=garbage`** → Blocked "unknown runtime; use auto, docker, or podman."
- **Probe times out on a cold Docker Desktop VM** → Blocked once; cooldown retry passes
  (VM is warm by then).
- **Node image missing** → only node runs are refused (with the pull command); python/bash
  unaffected. No `docker run` is ever attempted for a missing image (S5-D3).
- **Image deleted mid-session** (after its preflight passed) → `--pull=never` makes
  `docker run` fail fast instead of pulling; the stale cache entry is dropped, so the next
  call re-checks and returns the pull message.
- **Two calls race the first probe** → the lock makes one probe; both get its verdict.
- **Probe leftovers** → the probe file is deleted by the script itself; the probe container
  goes through the normal step-4 cleanup/reaper. Nothing new to leak.
- **Rejected probe candidates** (found at build time, the hard way) → every candidate
  backend the selector rejects is `drain()`ed — a bounded wait for its detached
  cleanup/reaper tasks — before selection continues or refuses. Leaving them running let
  event-loop shutdown mass-cancel a task mid-`create_subprocess_exec`, which can orphan the
  spawn waiter on Windows (CPython proactor wart) and deadlock loop close: the test suite
  hung in teardown, diagnosed via py-spy + a pending-task watchdog dump.
- **Podman** → selected only if its probes pass (D4); still documented experimental.

## 6. Tests

Docker-free (monkeypatched CLI/probe — run everywhere):
1. `VESTIBULE_BACKEND=naive` → NaiveBackend, zero CLI calls.
2. Runtime resolution: auto prefers Docker; auto falls through to Podman; forced runtime
   tries only itself; garbage value → legible refusal.
3. Daemon down → Blocked with "start" guidance; immediate retry hits the cache; after the
   cooldown (fake clock) it re-probes.
4. Missing image → Blocked containing the exact `docker pull` command.
5. Probe pass → `container`; full-fail + soft-pass → `container-degraded` with detail;
   both fail → every `run_code` Blocked (criterion 12: no silent naive, never `native`).
6. `_build_command` omits exactly the disabled soft flags (tmpfs mount itself survives).
7. Concurrent first calls → exactly one probe.
8. Server renders a degraded result as
   `isolation: container-degraded (limits not applied: …)` (criterion 11).
9. Missing image → run refused with the exact pull command **and no `docker run` is ever
   spawned** (S5-D3); preflight result cached (second run: no second `image inspect`).
10. `_build_command` contains `--pull never`; soft-disabled commands drop exactly the four
    soft flags (tmpfs itself survives; hard tier untouched).
11. Exit 125 reports `isolation: none` + "nothing was executed"; the server's
    `note_result` hook drops the cached selection on it (and never on naive selections).

Docker-marked (this machine):
12. Real selection picks Docker → verdict `container`; post-probe hello reports
    `isolation: container`.
13. After selection, no probe container survives and no probe file is left in the workspace.
14. Read-only-workspace mode: the probe demands reads work AND writes fail → still `container`.

Gate: all 76 existing tests stay green; `ruff check` + `mypy` clean.

## 7. Definition of done

Code + tests as one commit (`feat: M1 step 5 — …`); `docs/HISTORY.md` gains the S5-D1/D2/D3
sign-off bullets and the step-5 done bullet; contract §5 annotated; CLAUDE.md changelog on
session-log request.
