# M1 step 5 plan — backend selection & capability probing

Status: **draft — S5-D1 / S5-D2 / S5-D3 need sign-off.**
Parent contract: `docs/plans/M1-container-backend.md` §5 (binding; S5-D1/S5-D3 amend it slightly).

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

**The probe script** (bash — runs in `python:3.12-slim`, which ships bash):
- Normal mode: write a file in `/workspace`, read it back, delete it, print a marker.
- Read-only mode (`VESTIBULE_WORKSPACE_RO=1`): confirm reading works **and writing fails**.
- Pass = exit 0 + marker in stdout. Probe timeout 10 s (module constant, no new config).

The whole selection happens *before* the server's per-run outer deadline starts, so a slow
first probe (cold Docker Desktop VM) can't eat a run's time budget. Concurrent first calls
share one probe via an `asyncio.Lock`.

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

### S5-D3 — Selection only requires the python image
The probe runs in `python:3.12-slim` (it also serves bash). A missing **node** image does
not block selection — a node run just gets its own per-call "pull node:22-slim" message.
Full per-language image preflight (plus digest pinning and `--pull=never`, which also closes
the current accidental-auto-pull gap in `docker run`) is **step 6**, as planned.

## 4. Code changes

| File | Change |
|---|---|
| `src/vestibule/backends/select.py` | **New.** `select_backend(limits)` — the §2 checklist; cached verdict + 30 s failure cooldown; probe helper. |
| `src/vestibule/backends/container.py` | Constructor gains `soft_disabled` (frozenset). `_build_command` skips those flags (tmpfs stays, only its `size=` cap drops). Results report `container-degraded` + detail when any are off. |
| `src/vestibule/server.py` | `get_warden()` → async, returns the cached selection; selection failure raises `RunRefusedError` (built in step 4 for exactly this) → existing `Blocked:` path. Called *outside* the outer deadline. |
| `tests/test_select.py` | **New** — Docker-free suite (§6). |
| `tests/test_container.py` | +2 Docker-marked selection tests. |
| `docs/plans/M1-container-backend.md` | §5 annotated as amended by this doc. |
| `docs/HISTORY.md` | Sign-off + build bullets. |

No new config knobs. New module constants: probe timeout 10 s, failure cooldown 30 s.

## 5. What can go wrong (and what happens)

- **Daemon down** → Blocked "start Docker Desktop"; re-checked after 30 s (S5-D1).
- **Docker dies *after* a good probe** → the run comes back "runtime unavailable"
  (step 4's S4-D2 honesty path); that result also drops the cached selection, so the next
  call re-probes and gives the actionable message instead of failing forever.
- **`VESTIBULE_RUNTIME=garbage`** → Blocked "unknown runtime; use auto, docker, or podman."
- **Probe times out on a cold Docker Desktop VM** → Blocked once; cooldown retry passes
  (VM is warm by then).
- **Two calls race the first probe** → the lock makes one probe; both get its verdict.
- **Probe leftovers** → the probe file is deleted by the script itself; the probe container
  goes through the normal step-4 cleanup/reaper. Nothing new to leak.
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

Docker-marked (this machine):
9. Real selection picks Docker → end-to-end hello reports `isolation: container`.
10. After selection, no probe container survives and no probe file is left in the workspace.

Gate: all 76 existing tests stay green; `ruff check` + `mypy` clean.

## 7. Definition of done

Code + tests as one commit (`feat: M1 step 5 — …`); `docs/HISTORY.md` gains the S5-D1/D2/D3
sign-off bullets and the step-5 done bullet; contract §5 annotated; CLAUDE.md changelog on
session-log request.
