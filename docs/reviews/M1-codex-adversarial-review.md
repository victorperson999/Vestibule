# M1 design — Codex adversarial review

- **Date:** 2026-07-02 (before M1 implementation began)
- **Reviewer:** Codex (GPT-5.4) via `codex:codex-rescue`, read-only pass over `docs/PLAN.md` §Milestone 1, `docs/ARCHITECTURE.md`, `CLAUDE.md`, and the M0 source.
- **Scope:** the Milestone 1 design as written — `ContainerBackend` (throwaway Docker/Podman container per run), startup capability detection, `read_workspace` with path jailing.
- **Verdict:** sound to implement **with amendments** — no structural rework, but the written design is underspecified enough that an implementer could pass the stated acceptance criteria while leaving disk exhaustion, orphaned containers, symlink jail escapes, Docker Desktop permission failures, CLI stdio corruption, and unenforced-but-claimed resource limits unresolved. M1 needs a sharper contract: exact container profile, exact cleanup/timeout semantics, exact image strategy, exact `read_workspace` jail rules, and capability reporting that distinguishes "full container profile applied" from "some Docker command ran."

---

## Ranked findings (most severe first)

1. **Gap:** M1 treats `--network none`, memory, CPU, pids, read-only rootfs, non-root user, and a bind mount as sufficient container isolation. That is incomplete for the threat model in `docs/PLAN.md` Milestone 1 and the "prompt-injected exfiltration / destructive commands / resource exhaustion" scope in `docs/ARCHITECTURE.md`.
   **Scenario:** A Python or Node payload exploits a runtime or kernel-facing surface while the container still has Docker's default Linux capabilities. Even without network, default caps are more than this workload needs. A compromised process has unnecessary privilege inside the container, weakening the "container" claim.
   **Recommendation:** Make the M1 container profile explicit: add `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--network=none`, `--read-only`, `--user <nonroot uid>:<gid>`, `--pids-limit`, `--memory`, `--cpus`, no `--privileged`, no devices, no Docker socket mount, no host PID/IPC/user namespace. Treat missing support for these flags as degraded or blocked, not silently "container".

2. **Gap:** Disk exhaustion is not covered by the acceptance criteria. Memory, CPU, and pids caps do not stop writes to the container writable layer, `/tmp`, or the workspace bind mount.
   **Scenario:** Guest code runs `python -c "open('/workspace/blob','wb').write(b'x'*500_000_000_000)"` or writes endlessly to `/tmp`. On Docker Desktop this can fill the VM disk or the host-backed workspace even though memory and pids tests pass.
   **Recommendation:** Define writable storage limits. Use `--read-only`, `--tmpfs /tmp:rw,nosuid,nodev,size=<small>` and, where supported, a container writable-layer quota such as Docker `--storage-opt size=...` only after verifying the storage driver supports it. Put explicit byte limits on captured stdout/stderr during collection, not only after decode. Consider per-run scratch storage separate from the persisted workspace.

3. **Gap:** The workspace bind mount is a deliberate host write channel, but the design does not define whether destructive commands are allowed to destroy workspace files. This matters because `docs/ARCHITECTURE.md` says the workspace is the single file channel in/out.
   **Scenario:** An agent asks the sandbox to test cleanup code; the guest runs `rm -rf /`. The read-only root survives, but `/workspace` is writable and the user's project checkout is deleted. M1 can still pass "cannot read outside workspace" and "container isolation" while destroying the intended working directory.
   **Recommendation:** State the policy explicitly. If workspace mutation is intended, document that the workspace is inside the blast radius. If not intended, mount the real workspace read-only and provide a per-run writable scratch dir, then expose only selected artifacts. For M1, at minimum make the run result/tool description honest: "code may modify/delete files in the configured workspace."

4. **Gap:** Timeout semantics through the Docker/Podman CLI are underspecified. Killing the `docker run` client process is not the same thing as killing the container.
   **Scenario:** `asyncio.wait_for(proc.communicate())` times out, the server kills only the local `docker` CLI process, and the container continues running detached or orphaned in the daemon. The MCP handler returns a timeout, but the memory bomb or fork bomb keeps consuming daemon/VM resources.
   **Recommendation:** Use named containers with unique IDs/labels per run. On inner timeout, call `docker kill <container>` or equivalent, then `docker rm -f <container>` under a separate short cleanup deadline. Do not rely on killing the CLI process. Use `--rm` as a convenience, not as the only cleanup mechanism.

5. **Gap:** The outer timeout in `src/vestibule/server.py` protects the MCP handler, but M1 does not specify what happens if the container daemon or CLI hangs during cleanup.
   **Scenario:** Docker Desktop is wedged. The backend awaits `docker kill` or `docker rm` forever. The server-level `asyncio.wait_for(... timeout_s + 5)` fires and returns "sandbox did not return in time," but the cleanup coroutine is cancelled mid-flight and the container remains.
   **Recommendation:** Make backend cleanup cancellation-resistant. Use bounded subprocess calls for `run`, `kill`, `rm`, and `inspect`. Shield final cleanup where appropriate, but cap it. Return content errors from handlers per `CLAUDE.md`, and separately schedule best-effort orphan cleanup by label on next startup.

6. **Gap:** Container names, labels, and concurrency behavior are not specified.
   **Scenario:** Two `run_code` calls start concurrently and both choose `vestibule-run` as the container name. One cleanup removes the other run's container, or one call fails with a name collision. Tests that run calls serially still pass.
   **Recommendation:** Generate cryptographically random per-run IDs. Use both `--name vestibule-<id>` and labels like `--label vestibule.run_id=<id>` and `--label vestibule.owner=<pid>`. On startup, clean stale containers matching the Vestibule label. Add a concurrency test with at least several simultaneous `run_code` calls.

7. **Gap:** PID-1 behavior inside the container is not addressed. A simple interpreter can become PID 1 and fail to reap grandchildren.
   **Scenario:** Bash or Node spawns background children that exit repeatedly. The guest main process remains alive, zombies accumulate until `--pids-limit` is hit, and innocent later operations fail. Alternatively, timeout sends a signal to PID 1 with unusual semantics and descendants survive until the daemon cleans up.
   **Recommendation:** Use Docker `--init` if available, or run through a tiny init already present in the image. If Podman compatibility makes `--init` inconsistent, document and test the chosen behavior. On timeout, kill the whole container, not just the guest PID.

8. **Gap:** `read_workspace` path jailing is easy to implement incorrectly, especially with symlinks created by prior guest runs.
   **Scenario:** Guest code creates `/workspace/out -> C:\Users\victo\.ssh` on Windows via a reparse point or creates `out -> /host/path` on Linux/macOS where possible through the bind mount. Later the model calls `read_workspace("out/id_rsa")`. A naive `Path.resolve().startswith(workspace)` or string prefix check can be bypassed or can race.
   **Recommendation:** Accept only relative paths. Reject absolute paths, drive letters, UNC paths, `..`, empty components, NUL, and Windows ADS syntax containing `:` after the first component. Resolve against the configured workspace, then verify containment using path APIs, not string prefix. Refuse symlinks/reparse points by default for M1. On POSIX use `openat`-style traversal with `O_NOFOLLOW` where possible; on Windows check reparse attributes and, if feasible, validate the final opened handle path. Document any residual TOCTOU risk.

9. **Gap:** Windows path semantics are not covered, even though Windows 11 + Docker Desktop is the primary dev reality.
   **Scenario:** `read_workspace("PROGRA~1/...")`, case variants, UNC paths, `C:relative`, `\\?\C:\...`, or `file.txt:secret` behave differently from POSIX expectations. A jail check written for Linux passes tests but reads the wrong Windows path or an alternate data stream.
   **Recommendation:** Implement Windows-specific path validation rather than relying solely on `pathlib` normalization. Disallow colons, drive-qualified paths, UNC/device prefixes, and reserved names. Compare normalized resolved paths with `os.path.commonpath`, then perform post-open validation where possible. Add Windows-specific tests for drive letters, UNC, ADS, case variants, and 8.3-style names if enabled.

10. **Gap:** Docker Desktop bind mounts introduce UID/GID and performance behavior that the design does not account for.
    **Scenario:** The container runs as UID 1000, but the Windows-mounted workspace appears owned/mapped in a way that prevents writes, or writes work but produce files with surprising ownership on Linux. Python tests pass on Linux but fail on Windows/macOS with permission errors or severe latency.
    **Recommendation:** Define the container user and workspace mount contract per platform. Test a write/read/delete round trip on startup capability detection, not just `docker version`. Prefer a fixed in-container workspace path like `/workspace`. If non-root write fails, report a loud degraded setup error with the exact host path and suggested fix instead of falling back to root silently.

11. **Gap:** Image choice and provisioning are unspecified. "Supports python, bash, node" in `src/vestibule/config.py` is a contract, but M1 does not say what image provides those runtimes.
    **Scenario:** Implementation uses `python:latest` for Python, `node:latest` for Node, and host `/bin/bash` assumptions for Bash. First run pulls huge mutable images, behaves differently next month, or fails offline. Acceptance tests pass on the implementer's cached images only.
    **Recommendation:** Pick an explicit image strategy before implementation. Either one pinned multi-runtime image for M1, or three pinned images by language. Pin by digest, not just tag, for repeatability. Preflight image availability at startup and return a clear "container image missing; run X" message. Do not auto-pull during `run_code` unless the UX explicitly accepts long first-call latency and CLI pull output is safely captured.

12. **Gap:** Docker/Podman parity is assumed but not designed.
    **Scenario:** The backend emits Docker flags that Podman handles differently, or rootless Podman cannot apply the requested CPU/memory limits because cgroup delegation is unavailable. The result still says `isolation: container`, satisfying the current acceptance wording while resource caps were not actually enforced.
    **Recommendation:** Detection must be capability-based, not binary-name-based. Probe the selected runtime for each required control: network none, memory limit, CPU limit, pids limit, read-only rootfs, tmpfs, non-root user, cap drop, no-new-privileges, bind mount writeability. If any required control fails, return a loud startup/runtime error or report a weaker isolation string/details. Do not report plain `container` unless the M1 required controls were applied.

13. **Gap:** The `isolation` field is too coarse for degraded container behavior. `src/vestibule/backends/base.py` allows only a string like `container`, while `CLAUDE.md` requires honest reporting.
    **Scenario:** Docker runs with `--network none` and non-root, but memory limits are ignored on a misconfigured runtime. Returning `isolation: container` technically matches acceptance but overclaims protection.
    **Recommendation:** Keep `isolation: container` only for the full approved M1 profile. Add result text for applied/degraded controls, e.g. `isolation: container` plus `limits: memory=applied,cpu=applied,pids=applied,network=none,rootfs=readonly`. If the result schema cannot change much, encode this in stderr/content clearly. Consider adding enum values like `container-degraded` if compatible with the project's contract.

14. **Gap:** The stdout invariant is easy to violate through the Docker CLI. `CLAUDE.md` forbids stdout writes except through MCP, and `src/vestibule/backends/naive.py` already learned to force `stdin=DEVNULL`.
    **Scenario:** On first run, `docker run` emits pull progress, warnings, or CLI hints. If stdout/stderr are inherited, JSON-RPC is corrupted. If stdin is inherited or `-i` is used, the Docker CLI or guest can read the MCP input stream and hang the session.
    **Recommendation:** For every Docker/Podman subprocess: `stdin=DEVNULL`, `stdout=PIPE`, `stderr=PIPE`, no TTY, no inherited handles. Avoid `-i`. Capture and truncate CLI output separately from guest output. Prefer preflight/pull outside tool calls or return explicit setup errors so image-pull chatter never touches stdout.

15. **Gap:** Read-only rootfs can break normal Python/Node behavior unless writable locations are deliberately provided.
    **Scenario:** Python tries to write `__pycache__`, `pip`/Node tries cache/temp paths, or shell scripts expect `/tmp`. The container fails with confusing permission errors even for harmless code, so implementers remove `--read-only` to make tests pass.
    **Recommendation:** Keep `--read-only`, but set safe writable env and mounts: `TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, possibly `HOME=/tmp/home`, and tmpfs mounts for `/tmp` and any required cache dir with size caps. Do not relax rootfs read-only to fix runtime convenience.

16. **Gap:** Network isolation acceptance can be satisfied too narrowly.
    **Scenario:** Tests only verify `curl https://example.com` fails. The container still has loopback, can talk to services accidentally mounted through Unix sockets, or inherits proxy-related env vars that cause confusing behavior. General Docker behavior: `--network none` removes external networking, but it does not by itself police mounted sockets or environment.
    **Recommendation:** Do not mount host sockets of any kind. Scrub proxy and credential env vars from the container environment. Test TCP egress, DNS resolution, and absence of Docker socket/SSH agent mounts. It is fine if loopback exists inside the isolated network namespace; document that it cannot reach host services under the intended profile.

17. **Gap:** Capability detection at startup conflicts with the M1/M2 boundary. `docs/PLAN.md` says M1 has capability detection including native on Linux, but native is explicitly M2.
    **Scenario:** A Linux user runs M1. Detection sees Linux and reports or selects "native" because the roadmap says native on Linux, but no native backend exists yet. The server either crashes or misreports stronger isolation than was applied.
    **Recommendation:** For M1, detection should choose only implemented backends. Report "native unavailable: not implemented in this build; using container" or "no isolated backend available." Never include `native` in the active backend path until M2 exists and passes its own checks.

18. **Gap:** Error handling for malformed timeout values is still fragile in the current server shape.
    **Scenario:** The model sends `"timeout_seconds": "abc"` or a huge non-integer object. `int(...)` raises in `_handle_run_code`; the top-level handler catches it and returns `Internal error: ...`. That preserves the session but gives a low-quality error and may skip intended validation semantics.
    **Recommendation:** Before M1, make argument validation total and boring: type-check timeout, code, and language explicitly; return `Blocked: timeout_seconds must be an integer from 1 to 60`. Keep this in the clean server process before any container subprocess is spawned.

19. **Gap:** Resource exhaustion by output volume is only addressed at formatting time in `src/vestibule/server.py`, not necessarily during subprocess collection.
    **Scenario:** Guest code writes 10 GB to stdout. `proc.communicate()` or equivalent accumulates bytes in memory before `_truncate()` runs. The host/server is harmed even though the final MCP content would be truncated.
    **Recommendation:** Stream-read stdout/stderr with byte caps, stop reading or kill the container once caps are exceeded, and return a truncation marker. This applies to both guest output and Docker CLI output. Do not rely on post-hoc string truncation.

20. **Gap:** Acceptance criteria do not require proving container cleanup after server crash.
    **Scenario:** The MCP host forcibly kills `vestibule-mcp` while a container is running. `--rm` may not complete if the client disappears at the wrong moment or if daemon state is awkward. Later runs accumulate stale containers or stale volumes.
    **Recommendation:** Label every container and run startup scavenging for old `vestibule.*` labels. Add a manual or automated test that starts a long-running sandbox, kills the server process, restarts, and verifies stale containers are removed or at least reported.

---

## What is fine as designed — do not over-engineer

- Using the Docker/Podman CLI via `asyncio.create_subprocess_exec` is acceptable for M1. The Docker SDK would add dependency weight and does not remove the hard isolation questions.
- Keeping exactly two tools, `run_code` and `read_workspace`, is correct. Do not add file-listing, package-install, network-toggle, or shell-session tools in M1.
- `--network none` is the right baseline for no egress. Do not build custom firewall policy for M1.
- The server-level outer timeout pattern in `src/vestibule/server.py` is the right shape; it just needs backend cleanup semantics beneath it.
- Non-root container execution is worth keeping even if it creates Windows/macOS bind-mount friction. Do not "fix" cross-platform permissions by silently running as root.
- Deferring native namespaces/cgroups/seccomp to M2 is correct. M1 should not half-build native isolation.

---

## Verdict (verbatim)

The M1 design is sound to implement only with amendments. The basic container-per-run fallback is the right structural choice for cross-platform adoption, but the current written design is too underspecified: an implementer could pass the listed acceptance tests while leaving disk exhaustion, orphaned containers, symlink jail escapes, Docker Desktop permission failures, CLI stdio corruption, and degraded runtime limits unresolved.

No major architectural replacement is needed before starting. What is needed is a sharper M1 contract: exact container profile, exact cleanup/timeout semantics, exact image strategy, exact read_workspace jail rules, and capability reporting that distinguishes "full container profile applied" from "some Docker command ran."
