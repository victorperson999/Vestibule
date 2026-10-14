# M1 contract — ContainerBackend + `read_workspace`

Status: **signed off — D1–D4 all approved by the user 2026-07-03 (see `docs/HISTORY.md`). This contract is now binding for M1 implementation.**
Steps 1–7 built; the acceptance suite passes every §9 criterion (record in §9, 2026-10-14). M1 is **accepted** after the live-agent check ([`M1-step7-acceptance.md`](M1-step7-acceptance.md) §8).
Inputs: `docs/PLAN.md` §Milestone 1, `docs/ARCHITECTURE.md`, `CLAUDE.md`, and the Codex adversarial review (`docs/reviews/M1-codex-adversarial-review.md`, 2026-07-02) whose 20 findings this contract incorporates. Finding numbers below refer to that document.

---

## 1. Scope

**In:**
- `ContainerBackend` — one throwaway container per `run_code` call, driven through the Docker/Podman CLI via `asyncio.create_subprocess_exec`.
- Backend selection with capability probing; honest `isolation:` reporting including a degraded state.
- The persistent **workspace** (new concept — M0's naive backend used throwaway temp dirs).
- `read_workspace` MCP tool with a hardened path jail.
- Server hardening from the review: total argument validation (finding 18), streaming output caps (finding 19).

**Out (explicitly not M1):**
- Native Linux warden (M2). Detection must never select or report `native` (finding 17).
- Audit log (M3), custom published runtime image (M4 candidate), full Podman validation (see D4), network policy beyond `--network none`, warm container pools.

---

## 2. Decisions — four PROVISIONAL (need sign-off), the rest fixed

### D1 (PROVISIONAL) — Workspace is a writable dedicated directory
Default `~/.vestibule/workspace` (created on first use), override via `VESTIBULE_WORKSPACE`. Mounted read-write at `/workspace` (container workdir). `VESTIBULE_WORKSPACE_RO=1` mounts it read-only. The `run_code` tool description states plainly: *"code may create, modify, or delete files in the workspace directory."* (Finding 3: the blast radius is now declared policy, and it defaults to a sandbox-owned dir, not user data.)
- Rejected: read-only + tmpfs-scratch-only (kills the persistent file output channel); per-call `workspace_write` param (grows the tool surface).

> **Amendment 2026-10-14 (M1 step 7, S7-D2):** guest code's blast radius is still the
> workspace, but host tools that follow links reach past it. A symlink a guest plants in
> `/workspace` is a real symlink on the host: on Windows with Docker Desktop, a relative
> target becomes a native NTFS symlink that a host `open()` follows out of the workspace.
> `read_workspace` refuses links. The `run_code` description tells the model to read and
> change workspace files only through Vestibule's tools, and the README tells the user to
> treat the workspace like an untrusted download and never point `VESTIBULE_WORKSPACE` at a
> folder other tools use. A structural fix (seccomp `symlink` denial) is an M2 candidate.

### D2 (PROVISIONAL) — Two pinned official images, no auto-pull
- `python` and `bash` run in **`python:3.12-slim`** (Debian-based; ships bash).
- `node` runs in **`node:22-slim`**.
- Both pinned **by digest** in `config.py` (env-overridable via `VESTIBULE_IMAGE_PYTHON` / `VESTIBULE_IMAGE_NODE`). Digests are captured at implementation time from a fresh `docker pull` + `docker inspect --format '{{index .RepoDigests 0}}'` — never invented, never `:latest` (finding 11).
- Missing image ⇒ `run_code` returns a content error with the exact `docker pull …` command. **No pulling inside a tool call** (multi-minute stall + CLI chatter risk, findings 11/14). README documents the two pulls as a setup step.
- Rejected: custom published image (M4 candidate — better UX but we'd own image CI + supply chain now); auto-pull at startup (risks MCP handshake timeouts).

### D3 (PROVISIONAL) — Tiered degradation: hard controls block, soft limits degrade
- **Hard tier** (any missing ⇒ `run_code` returns `Blocked: container runtime cannot enforce <control>; refusing to run without isolation`): `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only` rootfs, non-root `--user`, workspace mount write/read/delete round-trip (read-only round-trip in RO mode).
- **Soft tier** (missing ⇒ run proceeds, result reports `isolation: container-degraded` + `limits not applied: <names>`): `--memory`/`--memory-swap`, `--cpus`, `--pids-limit`, tmpfs size cap.
- `isolation: container` is reported **only** when the full profile applied (findings 12/13). Untrusted code never runs with no isolation by default; `NaiveBackend` becomes opt-in dev-only via `VESTIBULE_BACKEND=naive` (still reports `isolation: none`).

### D4 (PROVISIONAL) — Docker-first, runtime-agnostic
Backend code is runtime-agnostic (capability probes, not binary-name checks); `VESTIBULE_RUNTIME=auto|docker|podman` from day one (auto: prefer `docker`, else `podman`). M1 is tested/supported on Docker only; Podman runs iff its probes pass and is documented as experimental. Full Podman validation is a follow-up milestone item.

### Fixed decisions (review-driven, one defensible answer each)
- **D5** Container-per-run; no pooling. Cold-start cost is accepted and later becomes M2's benchmark baseline.
- **D6** `read_workspace(path)`: directory ⇒ listing, file ⇒ text content. One tool, one required param (§6).
- **D7** Concurrency: per-run cryptographically random ID (`secrets.token_hex(8)`); `--name vestibule-<id>` + labels; `asyncio.Semaphore(VESTIBULE_MAX_CONCURRENT, default 4)` (finding 6).
- **D8** `RunResult.isolation` enum gains `container-degraded`; new `isolation_detail: str | None` carries per-control status into the formatted result (finding 13).
- **D9** Code delivery: the script is written to a per-run host temp dir mounted **read-only** at `/sandbox`; command is `<interpreter> /sandbox/main.<ext>` with workdir `/workspace`. Not argv (256 KB code blows Windows command-line limits), not stdin (`-i` is forbidden, finding 14), not the workspace (would pollute the persistent channel).
- **D10** Every runtime-CLI subprocess: `stdin=DEVNULL, stdout=PIPE, stderr=PIPE`, no TTY (finding 14; extends the M0 stdin rule).

---

## 3. Container execution profile (exact, finding 1)

```
<runtime> run
  --name vestibule-<runid>
  --label vestibule.run=1 --label vestibule.run_id=<runid>
  --label vestibule.deadline=<epoch> --label vestibule.owner=<token>   # step-4 amendment (S4-D3)
  --rm --init                                   # --init: PID-1 zombie reaping (finding 7)
  --pull never                                  # step-5 amendment (S5-D3): no in-call auto-pull, ever (D2)
  --network none
  --cap-drop ALL
  --security-opt no-new-privileges
  --read-only
  --user <uid>:<gid>                            # Linux host: os.getuid()/os.getgid(); else 1000:1000
  --memory <mem_mb>m --memory-swap <mem_mb>m    # swap = mem ⇒ no swap escape
  --cpus <cpu_pct/100>
  --pids-limit <pids_max>
  --tmpfs /tmp:rw,nosuid,nodev,size=<tmpfs_mb>m # finding 2: disk cap; finding 15: writable /tmp
  -e HOME=/tmp -e TMPDIR=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e NODE_OPTIONS=  # HOME: step-7b amendment
  -v <workspace_host>:/workspace[:ro]
  -v <run_tmp_host>:/sandbox:ro
  --workdir /workspace
  <image@digest>
  timeout -s KILL <timeout_s + 90>              # step-7a amendment (S7-D5): self-deadline
  <interpreter> /sandbox/main.<ext>
```

> **Amendment 2026-10-11 (M1 step 7a, [`M1-step7-acceptance.md`](M1-step7-acceptance.md)
> S7-D5):** the guest command is wrapped in coreutils `timeout`, set to the same deadline
> the owner stamps on the `vestibule.deadline` label. This closes a gap: nothing inside a
> container bounded its lifetime. If the server was hard-killed (real MCP clients kill the
> process tree about 2 s after closing stdin), a `while True:` guest ran until some later
> server's reaper found it, or forever if none did. Now the container ends itself and the
> daemon's `--rm` removes it. It never fires in normal operation, because the server kills
> timed-out runs at `timeout_s + 5`.
>
> The residual risk is documented: the watchdog runs as the guest's uid, so a deliberately
> malicious guest can `SIGSTOP` it. Custom images must ship `timeout`.

> **Amendment 2026-10-14 (M1 step 7b, found by acceptance criterion 9; signed off by
> Victor):** `HOME=/tmp`, not `/tmp/home`. `/tmp` is a fresh, empty tmpfs, so `/tmp/home`
> never existed: `~/x` writes, `cd ~` and `git config --global` failed with ENOENT. `/tmp`
> always exists (it is the mount point) and is writable, and dotfiles count against the
> same capped tmpfs. Rejected: a second tmpfs at `/tmp/home` (another mount and size cap to
> verify), and a `sh -c 'mkdir -p'` wrapper (another process in front of every guest).

Never present: `--privileged`, `--device`, host PID/IPC/UTS/user namespaces, any socket mount (Docker socket, SSH agent), `-i`/`-t`. Environment is only what we pass with `-e` — no host env inheritance; proxy/credential vars are therefore structurally absent (finding 16). Writable-layer quota (`--storage-opt size=`) is **not** used in M1 — it's storage-driver-dependent; the read-only rootfs + capped tmpfs already bound non-workspace writes (finding 2).

Windows host note: the workspace path is passed as a native `C:\...` path (Docker Desktop translates); the fixed in-container path is always `/workspace` (finding 10).

## 4. Run lifecycle

> **Amended by `docs/plans/M1/M1-step4-lifecycle.md` (2026-07-04):** step 1's semaphore acquire is a
> *bounded* wait (5 s) ending in a legible `RunRefusedError` ⇒ `Blocked:` message (S4-D1), and
> step 6's age-based orphan reaping is replaced by owner-stamped `vestibule.deadline` labels
> after a Codex adversarial review found the age heuristic could kill live runs of servers with
> different timeout configs (S4-D3). Step 5's shielded cleanup is further amended (Codex P2,
> post-implementation): cleanup is fully detached and never delays the result; the concurrency
> slot is released only when cleanup completes. Step 4.4's on-expiry kill/rm is likewise fully
> detached (Codex adversarial follow-up 2026-07-05 — the serialized kill could still consume
> the whole outer budget on a wedged daemon): the timed-out result returns immediately and the
> finisher performs the kill; timed-out runs report `exit_code: -1`.

> **Amendment 2026-10-11 (M1 step 7a, S7-D1): shutdown and cancellation.** A cleanup task
> is never cancelled by anything that waits for it. Waits use `asyncio.wait`, because the
> MCP SDK delivers client cancels and disconnects through an anyio cancel scope, and a
> `gather`-based wait passed that cancellation into the cleanup.
>
> - The selector tracks every backend it builds and gives them **one** concurrency
>   semaphore.
> - On stdin EOF, `server._main` calls `SELECTOR.aclose()`. That removes every active
>   container in one batched `rm -f` (1.5 s), then waits up to 0.5 s for cleanup tasks
>   without cancelling them.
> - If anything is still pending, the server exits with `os._exit(0)` instead of
>   returning into `asyncio.run`'s cancel-all. That cancel-all was reproduced hanging
>   the server indefinitely, with its container still running.

1. Acquire semaphore. Generate run ID; write script to per-run temp dir.
2. Spawn `docker run …` (D10 stdio rules).
3. **Collect with streaming caps** (finding 19): read stdout/stderr incrementally; per-stream collection cap = `2 × max_output_bytes` bytes. Cap exceeded ⇒ kill the container early, flag truncation. Display truncation in `server.py` still applies on top.
4. **Inner timeout** = `timeout_s + 5s` startup grace on the collect step (container cold start happens inside `docker run`; the grace keeps a `timeout_s=10` request from being eaten by 2s of Desktop startup). On expiry: `docker kill <name>` (5s cap) → `docker rm -f <name>` (5s cap, "No such container" tolerated — races with `--rm`) → mark `timed_out`. **Never** just kill the CLI process (finding 4).
5. Cleanup (temp dir removal + container removal check) is cancellation-resistant: bounded `asyncio.shield`-ed steps; total backend budget ≤ `timeout_s + 15`. Server outer deadline becomes `timeout_s + 20` (finding 5).
6. **Orphan reaping** (findings 6/20), at backend init and best-effort after each run: `docker ps -a --filter label=vestibule.run` ⇒ remove all **exited/created/dead** matches; remove **running** matches only when older than `max_timeout_s + 30s` (age from `.State.StartedAt`) — age-based so concurrent Vestibule servers don't reap each other's live runs.

## 5. Backend selection & capability probing

> **Amended by `docs/plans/M1/M1-step5-selection.md` (signed off & built 2026-07-05):** failed
> selections are cached only 30 s, then re-checked (S5-D1 — "start Docker Desktop, then retry"
> works without an MCP session restart); the degraded retry drops all four soft limits at once
> (S5-D2); per-run image preflight + `--pull never` land in step 5, not step 6 (S5-D3, after a
> Codex adversarial review showed `docker run`'s default auto-pull violated D2); a run exiting
> 125 (the runtime's own "run failed" code — container never started) reports
> `isolation: none` and drops the cached selection so the next call re-probes; the probe is a
> real run through the normal run path, so it exercises preflight, the full §3 profile, and
> the workspace round-trip in one shot. The server outer deadline is now `timeout_s + 30` to
> fund the preflight's bounded worst case (§7).

> **Amendment 2026-10-11 (M1 step 7a, S7-D3 + plan §4.1):**
> - **Limits are verified.** The probe reads the container's own soft limits back from
>   its cgroup (`memory.max`, `memory.swap.max`, `pids.max`, `cpu.max`, and the `/tmp`
>   size). Plain `container` now also requires every applied soft limit to match what was
>   requested. A mismatch reports `container-degraded`, with detail
>   `limits not verified: …`.
> - **cgroup v1 fallback.** cgroup v1 has no readback, so a runtime warning
>   (`WARNING…`, `WARN[`, `level=warning`) is what degrades the verdict there.
> - **Random probe filename.** The probe's workspace file is
>   `.vestibule-probe-<random hex>`. With the old fixed name, one guest could block all
>   future selections by creating a directory there.

Lazy, once per process (guarded by `asyncio.Lock`), cached; runs on first tool call so the MCP handshake is never delayed. Sequence:

1. `VESTIBULE_BACKEND=naive` ⇒ NaiveBackend (dev-only, `isolation: none`). Otherwise:
2. Resolve runtime per `VESTIBULE_RUNTIME`; `docker version` (bounded) checks daemon reachability — daemon down ⇒ cached "unavailable" with an actionable message ("start Docker Desktop").
3. Image presence check (`docker image inspect`); missing ⇒ actionable pull message (D2).
4. **Full-profile probe**: run a trivial guest (`python -c` equivalent: `sh -c 'echo ok > /workspace/.vestibule-probe && rm /workspace/.vestibule-probe && echo ok'`, adjusted for RO mode) under the complete §3 profile. Success ⇒ `container`.
5. On failure, retry **without soft-tier flags**. Success ⇒ `container-degraded`, recording which soft limits are off (D3). Failure ⇒ hard-tier unsupportable ⇒ backend unavailable; every `run_code` returns the Blocked explanation. Loud stderr logging at every step; misconfiguration is never silent, and `isolation:` never overclaims (findings 12/13/17).

The workspace round-trip in the probe covers Docker Desktop bind-mount permission failures up front (finding 10).

## 6. `read_workspace` (findings 8/9)

Schema: `{ "path": string, default "." }` — relative to the workspace root only.

**Reject before touching the filesystem** (each with a legible `Blocked:` message): absolute paths (POSIX or Windows), backslash-normalized then split; any `..` or empty component; NUL; any `:` anywhere (kills drive letters, `C:relative`, and ADS `file.txt:stream`; legitimate colon-filenames are acceptable collateral — documented); UNC/device prefixes (`//`, `\\`, `\\?\`, `\\.\`); Windows reserved device names (`CON`, `NUL`, `COM1`…); components with trailing dots/spaces.

**Then resolve and verify:** join to the workspace root, walk each component refusing symlinks/reparse points anywhere in the chain (guest-planted symlinks are the headline attack — finding 8); final containment check with `os.path.commonpath` against the resolved workspace root, never string prefix. POSIX opens use `O_NOFOLLOW`; on Windows, reparse attributes are checked pre-open. Residual TOCTOU (check-then-open race against a concurrently running guest) is documented in the tool description and SECURITY.md later — not silently ignored.

**Results:** directory ⇒ listing (name, type, size; capped at 200 entries); file ⇒ content decoded `errors="replace"`, capped at `max_output_bytes` with a truncation marker; missing path ⇒ `Not found: <relpath>` as content. Handler never raises.

Jail logic lives in a new `src/vestibule/workspace.py` so it is unit-testable without Docker.

## 7. Server changes

- **Validation made total** (finding 18): explicit type checks for `language`/`code`/`timeout_seconds` (`bool` excluded from int check); out-of-type ⇒ `Blocked: timeout_seconds must be an integer from 1 to <max>` — before any subprocess exists.
- `get_warden()` ⇒ cached `select_backend()` per §5. Naive is never auto-selected once M1 lands.
- Outer deadline `timeout_s + 20` (§4) — **raised to `timeout_s + 30` by the step-5 amendment**
  (image preflight adds a bounded 5 s worst case to the run path).
- `list_tools` gains `read_workspace`; `run_code` description updated: workspace at `/workspace` persists **and is writable by guest code** (D1 honesty), everything else ephemeral, no network.
- `_format_result` renders `isolation_detail` when present.

## 8. Config additions (`config.py`)

| Field | Env | Default |
|---|---|---|
| `workspace_dir` | `VESTIBULE_WORKSPACE` | `~/.vestibule/workspace` |
| `workspace_ro` | `VESTIBULE_WORKSPACE_RO` | off |
| `runtime` | `VESTIBULE_RUNTIME` | `auto` |
| `backend` | `VESTIBULE_BACKEND` | `auto` (`naive` = explicit dev opt-in) |
| `image_python` | `VESTIBULE_IMAGE_PYTHON` | `python:3.12-slim@sha256:<pinned at impl time>` |
| `image_node` | `VESTIBULE_IMAGE_NODE` | `node:22-slim@sha256:<pinned at impl time>` |
| `max_concurrent` | `VESTIBULE_MAX_CONCURRENT` | 4 |
| `tmpfs_mb` | `VESTIBULE_TMPFS_MB` | 64 |

(`Limits.from_env` grows a string-valued getter alongside `_i`.)

> **Amendment 2026-10-10 (M1 step 6, `docs/plans/M1/M1-step6-pinning.md` S6-D1):** the image
> defaults shipped **tagless and fully qualified** —
> `docker.io/library/python@sha256:a6e34c59…` (from `python:3.12-slim`) and
> `docker.io/library/node@sha256:c3de60bf…` (from `node:22-slim`), full digests in
> `config.py`. The `name:tag@sha256:` form in the table above was dropped because Docker
> does not verify a tag next to a digest; the tag lives in the `config.py` comment, and a
> Docker test checks the interpreter versions inside the pinned images. Captured from a
> fresh pull on 2026-10-10 and proven under both Docker Desktop image stores (S6-D4).

## 9. Acceptance criteria (supersedes PLAN.md §M1 list)

1. python/bash/node hello-world via Docker ⇒ `isolation: container`.
2. Network: TCP connect **and** DNS resolution fail from the guest (not just one `curl`) (finding 16).
3. Memory bomb contained (guest OOM-killed; host unaffected).
4. Fork bomb contained (`--pids-limit`; host unaffected).
5. Disk: writing past the tmpfs cap fails inside the guest; rootfs writes fail (`--read-only`) (finding 2).
6. Timeout: infinite-loop guest ⇒ result within `timeout_s + 20`, marked timed out, **and `docker ps -a` shows no surviving container** (finding 4).
7. Concurrency: 4 simultaneous `run_code` calls all succeed with distinct containers (finding 6).
8. Orphan reaping: a pre-planted stale labeled container is removed at backend init (finding 20).
9. Innocent code works under `--read-only`: multi-module python script with imports runs clean (no `__pycache__` failure) (finding 15).
10. Jail unit suite passes without Docker: `..`, absolute, drive-letter, UNC, ADS, reserved-name, trailing-dot, symlink cases — plus one integration test where **guest code plants a symlink** and a following `read_workspace` refuses it (findings 8/9).
11. Degraded path (probe forced via test injection): result reports `container-degraded` + the missing limits, never plain `container` (findings 12/13).
12. No runtime/daemon/image ⇒ `run_code` returns an actionable Blocked/setup message; never silent naive fallback; never `native` (finding 17).
13. Malformed args (`timeout_seconds: "abc"`, non-string code) ⇒ `Blocked:` messages, session survives (finding 18).
14. `ruff` + `mypy` clean; non-Docker tests run everywhere; Docker-marked tests pass on this machine (Docker Desktop 28.5.1).

**Acceptance record (2026-10-14, M1 step 7b).** Every criterion has a passing test, on
Windows 11 with Docker Desktop 28.5.1 (containerd image store). The Docker tests go through
`server.call_tool`, the dispatch an agent's call reaches, with a fresh selector each, so
selection, preflight, the run and result formatting are all in the path. Every result is
checked against the honest isolation values (`native` can never appear).

| # | Evidence (`tests/`) | What was observed |
|---|---|---|
| 1 | `test_acceptance.py::test_c01_*`; `test_stdio_e2e.py::test_stdio_happy_path_and_clean_exit` | python, bash, node ⇒ `isolation: container`, also over a real stdio pipe |
| 2 | `test_c02_*` | TCP to an IP, `getaddrinfo`, and a UDP send all fail; the only interface is `lo` |
| 3 | `test_c03_*` | a forked child touching ~1 GiB dies by SIGKILL, `memory.events` `oom_kill` ≥ 1, the parent reports, the next run works on the same selection |
| 4 | `test_c04_*` | forks refused with `EAGAIN` at 125 of 128 pids (tini, `timeout` and the interpreter hold 3); the classic bash bomb hits the cap, the run returns, no container survives, the next run works |
| 5 | `test_c05_*` | `/tmp` takes exactly 64 MiB, then `ENOSPC`; creating a file in world-writable `/var/tmp` and in `/sandbox` fails with `EROFS`; `/` is mounted `ro` |
| 6 | `test_c06_*` | a busy loop with a 3 s timeout returns `[timed out …]` in ~8 s (≤ 23 s), and no container survives |
| 7 | `test_c07_*` | 4 concurrent calls succeed with 4 distinct container hostnames |
| 8 | `test_c08_*` | a planted container with a past deadline is removed after one ordinary call |
| 9 | `test_c09_*` | a package saved to `/workspace` imports with the documented idiom, Node `require('/workspace/…')` works, stdlib imports, `$HOME` and `/tmp` are writable, and no `__pycache__` is written. **Found the `$HOME` bug amended in §3.** |
| 10 | `test_workspace.py` (unit suite); `test_c10_*` | guest-planted relative, absolute and `..` links are listed as `symlink` and refused, and the host canary never appears |
| 11 | `test_c11_*` | a real injection (a 1 MB memory limit Docker refuses) ⇒ `container-degraded (limits not applied: cpu, memory, pids, tmpfs-size)` |
| 12 | `test_c12_*` (2 tests) | a missing runtime ⇒ install-and-restart `Blocked:`; a never-pulled digest ⇒ `Blocked:` with its exact `docker pull`, and no `run` or `pull` is ever spawned |
| 13 | `test_validation.py` | malformed arguments ⇒ `Blocked:` (mapped; unchanged) |
| 14 | the gate | ruff + mypy clean, full suite green; with Docker hidden from `PATH`, every Docker test skips with its reason and everything else passes |
| — | `test_hard_and_soft_controls_apply_inside_the_guest` | from inside: uid 1000; CapEff/Prm/Bnd/Amb all 0; `NoNewPrivs: 1`; `Seccomp: 2`; env = the profile's 4 + the image's own + `HOSTNAME`; no runtime socket; `/` read-only; `memory.max` 256 MiB, `memory.swap.max` 0, `pids.max` 128, `cpu.max` `75000 100000`, `/tmp` 64 MiB |

"Host unaffected" (criteria 3–4) is asserted as what can be observed from here: on Docker
Desktop the guest runs in a Linux VM, so host memory and PIDs are not measurable. The
guest was stopped and the cgroup says why, the server returned a normal result, the next
run works, and no container survived. Criterion 14 is proven on a Windows host only; the
Linux-only paths (`os.getuid`, `O_NOFOLLOW`) first run when M2 work moves to Linux/WSL.

## 10. Implementation order (each step ≈ one commit)

1. `config.py` additions + `workspace.py` jail + full jail unit suite (no Docker needed).
2. Server validation hardening + `read_workspace` wiring + `RunResult` extension.
3. `ContainerBackend` happy path (§3 profile, D9 script mount, streaming collection).
4. Timeout/kill/cleanup, orphan reaping, concurrency semaphore (§4).
5. Capability probing + tiered selection + honest reporting (§5).
6. Digest pinning + image preflight + setup UX messages.
7. Docker-marked acceptance suite (§9) + README/docs update.
