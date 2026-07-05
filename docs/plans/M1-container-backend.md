# M1 contract — ContainerBackend + `read_workspace`

Status: **signed off — D1–D4 all approved by the user 2026-07-03 (see `docs/HISTORY.md`). This contract is now binding for M1 implementation.**
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
  --network none
  --cap-drop ALL
  --security-opt no-new-privileges
  --read-only
  --user <uid>:<gid>                            # Linux host: os.getuid()/os.getgid(); else 1000:1000
  --memory <mem_mb>m --memory-swap <mem_mb>m    # swap = mem ⇒ no swap escape
  --cpus <cpu_pct/100>
  --pids-limit <pids_max>
  --tmpfs /tmp:rw,nosuid,nodev,size=<tmpfs_mb>m # finding 2: disk cap; finding 15: writable /tmp
  -e HOME=/tmp/home -e TMPDIR=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e NODE_OPTIONS=
  -v <workspace_host>:/workspace[:ro]
  -v <run_tmp_host>:/sandbox:ro
  --workdir /workspace
  <image@digest>
  <interpreter> /sandbox/main.<ext>
```

Never present: `--privileged`, `--device`, host PID/IPC/UTS/user namespaces, any socket mount (Docker socket, SSH agent), `-i`/`-t`. Environment is only what we pass with `-e` — no host env inheritance; proxy/credential vars are therefore structurally absent (finding 16). Writable-layer quota (`--storage-opt size=`) is **not** used in M1 — it's storage-driver-dependent; the read-only rootfs + capped tmpfs already bound non-workspace writes (finding 2).

Windows host note: the workspace path is passed as a native `C:\...` path (Docker Desktop translates); the fixed in-container path is always `/workspace` (finding 10).

## 4. Run lifecycle

> **Amended by `docs/plans/M1-step4-lifecycle.md` (2026-07-04):** step 1's semaphore acquire is a
> *bounded* wait (5 s) ending in a legible `RunRefusedError` ⇒ `Blocked:` message (S4-D1), and
> step 6's age-based orphan reaping is replaced by owner-stamped `vestibule.deadline` labels
> after a Codex adversarial review found the age heuristic could kill live runs of servers with
> different timeout configs (S4-D3). Step 5's shielded cleanup is further amended (Codex P2,
> post-implementation): cleanup is fully detached and never delays the result; the concurrency
> slot is released only when cleanup completes.

1. Acquire semaphore. Generate run ID; write script to per-run temp dir.
2. Spawn `docker run …` (D10 stdio rules).
3. **Collect with streaming caps** (finding 19): read stdout/stderr incrementally; per-stream collection cap = `2 × max_output_bytes` bytes. Cap exceeded ⇒ kill the container early, flag truncation. Display truncation in `server.py` still applies on top.
4. **Inner timeout** = `timeout_s + 5s` startup grace on the collect step (container cold start happens inside `docker run`; the grace keeps a `timeout_s=10` request from being eaten by 2s of Desktop startup). On expiry: `docker kill <name>` (5s cap) → `docker rm -f <name>` (5s cap, "No such container" tolerated — races with `--rm`) → mark `timed_out`. **Never** just kill the CLI process (finding 4).
5. Cleanup (temp dir removal + container removal check) is cancellation-resistant: bounded `asyncio.shield`-ed steps; total backend budget ≤ `timeout_s + 15`. Server outer deadline becomes `timeout_s + 20` (finding 5).
6. **Orphan reaping** (findings 6/20), at backend init and best-effort after each run: `docker ps -a --filter label=vestibule.run` ⇒ remove all **exited/created/dead** matches; remove **running** matches only when older than `max_timeout_s + 30s` (age from `.State.StartedAt`) — age-based so concurrent Vestibule servers don't reap each other's live runs.

## 5. Backend selection & capability probing

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
- Outer deadline `timeout_s + 20` (§4).
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

## 10. Implementation order (each step ≈ one commit)

1. `config.py` additions + `workspace.py` jail + full jail unit suite (no Docker needed).
2. Server validation hardening + `read_workspace` wiring + `RunResult` extension.
3. `ContainerBackend` happy path (§3 profile, D9 script mount, streaming collection).
4. Timeout/kill/cleanup, orphan reaping, concurrency semaphore (§4).
5. Capability probing + tiered selection + honest reporting (§5).
6. Digest pinning + image preflight + setup UX messages.
7. Docker-marked acceptance suite (§9) + README/docs update.
