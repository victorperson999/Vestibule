# Security: what Vestibule protects, and what it doesn't

Vestibule runs code that an AI agent wrote, inside a sandbox on your own machine. This file says
what that sandbox is built from, what it leaves open, and how to report a problem. Each claim
ends with where it comes from: a test, a signed plan, or a row in
[`docs/BUILD_LOG.md`](docs/BUILD_LOG.md)'s known issues. The test suite checks that every test
and file named here exists. It doesn't check that a section, a decision or a row inside a file
still says what is claimed here.

**Read this first:**
- Vestibule is in early development. No one independent of this project has reviewed or audited
  this code.
- It is not a virtual machine. On Linux the sandbox shares your kernel.
- It sandboxes only the code an agent sends to its `run_code` tool. An agent's own shell and file
  tools still run on your machine.

## 1. What it's for

**The risks it's built against,** for code sent to `run_code`: a prompt-injected run that tries
to read your secrets and send them out, a run that deletes or overwrites the wrong files, and a
run that eats the machine's memory, CPU or process table.

**What holds.** For a run whose result says `isolation: native` or `isolation: container`, these
are out of the run's reach, short of a bug in the kernel or the container runtime:
- your files outside the workspace folder;
- the network;
- the machine's memory, CPU and processes beyond the run's caps.

**What stays possible** in such a run:
- deleting or changing anything in the workspace, which is writable by design;
- filling your disk through the workspace (§3.2);
- printing whatever it can read back to the agent, which then holds it;
- leaving a link in the workspace that some other tool on your machine follows (§3.1).

A result that says `native-degraded` or `container-degraded` names the caps it is missing, and
`none` means no isolation (§2).

**What it isn't:**
- **Not a VM.** A kernel bug that lets a process escape a namespace would let it escape this.
  On Windows and macOS the container backend runs inside Docker Desktop's own Linux VM, which is
  Docker's boundary, not Vestibule's.
- **Not a guard on the agent's other tools.** An agent with a shell tool can run code outside
  Vestibule. The README shows a session that leaves the agent no built-in tools, and what to
  add so that other MCP servers are left out too.

**Not tested:** arm64 Linux hosts; Podman; and a sandboxed run on macOS. On a macOS runner the
tests that need no Docker pass (849 passed, 29 skipped for stated reasons, 2026-11-09), and that
runner has no Docker to run a container with.

Sources: the tests cited in §2 for each backend; [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
"Honest scope"; for what wasn't tested, the README's "Where native works" and
[`docs/BUILD_LOG.md`](docs/BUILD_LOG.md)'s M4 step 4a entry.

## 2. The boundary

Every result of a run ends with an `isolation:` line saying what protected it. A `Blocked:`
reply ran nothing and has none.
- **A native run is judged on its own,** from what the kernel reported for that run.
- **A container run takes the verdict of the check made when the backend was chosen:** a real
  run with the full set of flags (for `container-degraded`, possibly a second one with the
  resource caps left off).
  - On a cgroup v2 host that run read back, from inside, the memory, CPU and process caps and
    `/tmp`'s size. On cgroup v1 it can't, and the runtime's own warnings are the only sign that
    a cap failed.
  - On both it checks whether the workspace is writable.
  - The other flags are applied by the runtime and not read back on your machine (§3.14).
  - The verdict drops to `none` when the runtime reports exit code 125, which usually means the
    container didn't start.

### `native` (Linux, no Docker, no root)

- **Namespaces:** new user, PID, mount, network, UTS, IPC and cgroup namespaces. The code runs as
  your own uid, never root, with every capability dropped and `no_new_privs` set.
  `tests/test_native_acceptance.py::test_c10_the_guests_own_view`
- **A filesystem jail:** `pivot_root` into a read-only copy of the pinned image, plus the
  workspace, a size-capped `/tmp`, and a read-only folder holding the script. None of your other
  files exist inside.
  `tests/test_native_acceptance.py::test_c02_no_host_filesystem_is_visible`,
  `tests/test_native_acceptance.py::test_c07_tmp_is_capped_and_the_rootfs_is_read_only`
- **No network:** the network namespace has only `lo`. `AF_VSOCK`, which a network namespace
  doesn't isolate, is refused by the syscall filter.
  `tests/test_native_acceptance.py::test_c03_no_network_and_no_vsock`
- **Limits:** each run gets its own systemd scope with cgroup v2 caps on memory (no swap), CPU and
  processes.
  `tests/test_native_acceptance.py::test_c04_a_memory_bomb_is_killed_and_the_next_run_works`,
  `tests/test_native_acceptance.py::test_c05_a_fork_bomb_stops_at_pids_max`,
  `tests/test_native_acceptance.py::test_c06_a_busy_loop_stays_under_the_cpu_limit`
- **A syscall allowlist (seccomp).** It is derived from Docker's default profile, the
  `moby/profiles` file at commit `6fe7deb` (Apache-2.0), shipped unchanged in the package and
  compiled by the system's libseccomp.
  - A named set of dangerous calls (`mount`, `unshare`, `setns`, `bpf`, `keyctl`, `reboot` and
    others) fails and is listed in the result.
  - As in Docker's profile, `ptrace` is allowed, so a debugger works on processes in the same
    sandbox.
  - `tests/test_native_acceptance.py::test_c09_denials_are_named_and_the_storm_stays_in_the_envelope`,
    `tests/test_native_seccomp.py::test_ptrace_policy_is_allow_and_io_uring_is_never_notified`,
    [`docs/plans/M2/M2-step5-seccomp.md`](docs/plans/M2/M2-step5-seccomp.md) S5-D9
- **The verdict.** A run is `native` only if every check held: the filter is loaded, and the
  namespaces, the identity maps and the mount table are the ones the server set up. If one of
  those fails, the result says `none`. If they hold and only a resource cap is missing, it says
  `native-degraded`. `tests/test_native_verdict.py`

### `container` (Docker; the backend on Windows and macOS)

Each run is one throwaway container, started with:
- `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`;
- a non-root `--user`;
- `--memory` with the same `--memory-swap`, `--cpus`, `--pids-limit`, and a size-capped `/tmp`;
- `--pull never` and an image named by digest, so a call never downloads anything;
- the workspace and a read-only script folder as its only mounts;
- a `timeout` around the code, so the container ends itself at its deadline.

`tests/test_acceptance.py::test_c02_tcp_dns_and_udp_all_fail`,
`tests/test_acceptance.py::test_c03_memory_bomb_is_oom_killed_and_the_sandbox_carries_on`,
`tests/test_acceptance.py::test_c04_fork_bomb_is_capped_and_the_sandbox_carries_on`,
`tests/test_acceptance.py::test_c05_tmpfs_is_capped_and_the_rootfs_is_read_only`,
`tests/test_acceptance.py::test_hard_and_soft_controls_apply_inside_the_guest`,
[`docs/plans/M1/M1-container-backend.md`](docs/plans/M1/M1-container-backend.md) §3

### `native-degraded` and `container-degraded`

The run was isolated, but one or more resource caps couldn't be applied or verified. The result
names them and says why. A `native-degraded` run with no cgroup of its own has no memory or CPU
cap at all (§3.9).
`tests/test_acceptance.py::test_c11_unenforceable_limits_report_container_degraded`,
`tests/test_native.py::test_without_a_bus_the_run_degrades_and_the_backstop_holds`

### `none`

No isolation was confirmed. It doesn't always mean nothing ran. It appears when:
- the development-only `naive` backend was chosen by hand: the code ran on your machine;
- nothing was executed, and the result says so: the container runtime is missing or didn't
  start the container, or the native sandbox couldn't be set up. A container that didn't start
  shows as exit code 125, and code that exits with 125 itself is reported the same way, though
  it ran;
- a native sandbox was built and one of the server's checks then failed: the code ran, without
  the isolation confirmed;
- the sandbox never reported back: code may have run.

`tests/test_native_verdict.py`,
[`docs/plans/M1/M1-step4-lifecycle.md`](docs/plans/M1/M1-step4-lifecycle.md) S4-D2

### `read_workspace`

The second tool reads a file or lists a folder in the workspace, from outside the sandbox.
- **On a local drive, on Linux and Windows,** it opens each part of a path once, from the folder
  it already holds open, and never follows a link. A guest changing the workspace during the
  call can make it fail, or get it a file's older content. It can't make the tool return, or ask
  about, anything outside the workspace. macOS takes the Linux code, and its tests pass on a
  macOS runner, except one that can't be set up there: its filesystem refuses the file name
  that test needs.
- A file's content can change while it's read.
- What it leaves is §3.12.

`tests/test_workspace.py`,
`tests/test_acceptance.py::test_c10_guest_planted_symlinks_are_refused_by_read_workspace`,
[`docs/plans/M4/M4-step4-workspace-race.md`](docs/plans/M4/M4-step4-workspace-race.md) §8

## 3. Known residuals

1. **Links a run leaves in the workspace are real links on your machine.** Any tool that
   follows one can read or write outside the workspace: an editor, a backup, the agent's own
   shell. Picture `report.txt` pointing at `~/.ssh/id_ed25519`.
   - Inside the sandbox a link resolves in the sandbox's own view, and `read_workspace` refuses
     links.
   - On Windows with Developer Mode on, a guest's relative link became a real NTFS symlink when
     its target exists on the host. Without Developer Mode that wasn't tested.
   - Denying the `symlink` call was considered and rejected: it breaks `python -m venv` and
     npm's `.bin` links.
   - Don't point `VESTIBULE_WORKSPACE` at a folder other tools use.
   - Sources: BUILD_LOG's 2026-10-11 (S7-D2) row;
     [`docs/plans/M1/M1-step7-acceptance.md`](docs/plans/M1/M1-step7-acceptance.md) S7-D2;
     [`docs/plans/M4/M4-step0-spike.md`](docs/plans/M4/M4-step0-spike.md) A-3.
2. **The workspace has no size cap.** `/tmp` is capped and the rest of the sandbox is read-only,
   but a run can fill your disk through the workspace. A cap needs filesystem quotas or a
   loop-mounted image, and both need root. `VESTIBULE_WORKSPACE_RO=1` makes it read-only.
   Source: BUILD_LOG's 2026-10-14 (7b docs) row.
3. **Native sandboxes run outside your agent's own cgroup,** in your user manager's `app.slice`.
   A memory limit you put on the agent app doesn't count them. Each sandbox has its own caps.
   Source: [`docs/plans/M2/M2-step4-cgroups.md`](docs/plans/M2/M2-step4-cgroups.md).
4. **A native guest can learn some things about your machine,** as it could in a container:
   - your numeric uid and gid, the kernel version, and `/proc`'s CPU and memory information;
   - your username: the mount table shows the host paths of the image copy and the workspace.
     A container guest sees its own mounts the same way: the workspace's host path, and its
     script folder's, whose name carries the run's deadline and, on Linux and macOS, the
     server's pid;
   - your supplementary groups still count for permission checks, though nothing they unlock is
     visible inside the jail.
   - Sources: [`docs/plans/M2/M2-step3-warden-core.md`](docs/plans/M2/M2-step3-warden-core.md)
     §5; [`docs/plans/M4/M4-step1-run-dir-sweep.md`](docs/plans/M4/M4-step1-run-dir-sweep.md) §7.
5. **Foreign-architecture syscalls rest on the filter's configuration, not on a live test.**
   The 32-bit ABI is refused by the filter's architecture check, which two unit tests pin. The
   x32 ABI shares the host's architecture token, so that check doesn't catch it; libseccomp's
   own handling does.
   Source: [`docs/plans/M2/M2-step7-acceptance.md`](docs/plans/M2/M2-step7-acceptance.md) S7-D2.
6. **With the container backend, a deliberately hostile run can outlive its deadline if the
   server is gone.** While the server lives, it stops a timed-out container from outside. If
   the server was killed mid-run, or its stop failed, only the timer inside the container is
   left. That timer runs as the guest's own user, so code written to stop it can keep its
   container alive until a later Vestibule run removes it. Meanwhile it stays inside the caps
   it was started with. A native sandbox is stopped from outside in every case, by processes the
   guest can't see. Sources: BUILD_LOG's 2026-10-11 (S7-D5) row;
   [`docs/plans/M1/M1-step7-acceptance.md`](docs/plans/M1/M1-step7-acceptance.md) S7-D5.
7. **A container can be left in the Created state** if the client kills the server in the 100 ms
   or so between the container's creation and its start. It never ran and uses no CPU or memory.
   A later run removes it. Source: BUILD_LOG's 2026-10-11 (7a review) row.
8. **On Windows, a run in flight when the client exits may live until its own deadline.** Claude
   Code closes the server's input, waits 300 ms, then kills its process tree. If that lands
   before the cleanup finishes, the container ends itself at its deadline (the timeout plus
   90 s). It was measured twice and not hit, and nothing rules it out.
   Source: BUILD_LOG's 2026-10-15 (M2 step 1b) row.
9. **Native without a working systemd user manager has no memory cap.** With
   `VESTIBULE_BACKEND=native` forced on such a host, runs report `native-degraded (limits not
   applied: …)`, and a memory bomb can set off your machine's own out-of-memory killer.
   - On Linux 5.14 and later a process limit still stops a fork bomb there. Before 5.14 nothing
     does.
   - `auto` prefers a full container over this. It still ends here when no container runtime is
     installed, when the runtime can't apply or verify its full set of protections, and, until
     the next check 30 s later, while the runtime is refusing (its daemon stopped, an image not
     pulled).
   - `tests/test_native.py::test_without_a_bus_the_run_degrades_and_the_backstop_holds`
10. **A server started as root refuses to run code** on Linux and macOS, with either sandbox
    backend, because the code would run as root too. Run Vestibule as your own user.
    `tests/test_select.py::test_a_server_running_as_root_runs_no_code`
11. **Membership of the `docker` group is equivalent to root** on a Linux host. That is Docker's
    design, and the container backend needs a Docker you can use. Rootless Docker avoids it and
    hasn't been tried with Vestibule. Ubuntu's Docker snap can't run Vestibule's containers.
    Source: [`docs/plans/M4/M4-adoption.md`](docs/plans/M4/M4-adoption.md) P3.
12. **`read_workspace`, beyond §2:**
    - **A Windows server whose workspace is on a network or WSL share** (`VESTIBULE_WORKSPACE`
      set to `\\wsl.localhost\…`): the far side looks every name up again, so the walk is by
      name there. No outside content came back in tests, because Windows doesn't follow that
      share's links. A guest that swaps a file for a FIFO at the right moment can hold one call
      for about 10 s. Other kinds of share weren't tried.
    - **A directory listing is of the first 10,000 entries read.** A folder that holds more is
      listed from those, in the filesystem's order and not by name, and the listing says so. It
      shows 200 entries at most and stops at the output cap, which is counted in characters:
      names made of invalid bytes can still come to about 55 KB.
    - **A guest's file names can't add lines to a listing.** Control and format characters in a
      name are shown as escapes. A name that isn't valid text shows a replacement character, so
      two such names can look alike. If a guest deletes entries while a listing runs, its count
      of entries not shown can include some that are gone. A file's content is returned as it
      is.
      `tests/test_workspace.py::test_a_line_break_in_a_name_cant_forge_an_entry`,
      `tests/test_workspace.py::test_a_folder_is_read_up_to_the_scan_cap`
    - Source: BUILD_LOG's 2026-11-09 row on shares;
      [`docs/plans/M4/M4-step4-followups.md`](docs/plans/M4/M4-step4-followups.md).
13. **Two machines sharing one remote Docker daemon, with clocks more than 60 s apart, could
    remove each other's containers early.** Vestibule is built for a local daemon.
    Source: [`docs/plans/M1/M1-step4-lifecycle.md`](docs/plans/M1/M1-step4-lifecycle.md) S4-D3.
14. **The container backend trusts the runtime for the flags it can't read back.** On your
    machine Vestibule reads back what §2 lists: the resource caps and `/tmp`'s size on a
    cgroup v2 host, and the workspace's mode on any host. It passes `--network none`, `--cap-drop ALL`, `no-new-privileges`, `--read-only` and
    `--user` to the runtime and relies on it to apply them: a runtime that refuses one fails
    the check, and one that silently ignored one would not be noticed. Vestibule also doesn't
    set the container's seccomp profile. The container gets the daemon's default, so a daemon
    configured to run containers unconfined gives a container run no syscall filter. The test
    suite checks all of these from inside a container, on the machines it ran on.
    `tests/test_acceptance.py::test_hard_and_soft_controls_apply_inside_the_guest`,
    `tests/test_acceptance.py::test_c02_tcp_dns_and_udp_all_fail`

## 4. Supply chain

- **The two sandbox images are pinned by digest** (`python:3.12-slim` and `node:22-slim` as of
  the last re-pin), so they can't change silently. The flip side: they get no upstream security
  fixes until Vestibule re-pins them in a release. `tests/test_pins.py`,
  [`docs/plans/M1/M1-step6-pinning.md`](docs/plans/M1/M1-step6-pinning.md)
- **`vestibule-mcp setup` is the only part of Vestibule that downloads anything,** and it
  downloads from Docker Hub. For the native sandbox it fetches the image contents itself and
  checks every byte against those pins before unpacking. For the container backend it asks
  Docker to pull the same digests. Vestibule has no account, no telemetry and no update check.
  `tests/test_registry.py`, `tests/test_rootfs.py`
- **An image override that isn't pinned by digest still works, and logs a warning.**
  `tests/test_select.py::test_unpinned_override_warns_for_both_refs`
- **Dependencies.** Vestibule has one direct dependency, the `mcp` SDK, limited to the minor
  version the tests ran on. Everything `mcp` needs is resolved fresh when you install: 30
  packages on Linux and 31 on Windows today, counting Vestibule and `mcp`. So what you get can
  differ from what the tests ran.
  Sources: [`docs/plans/M4/M4-adoption.md`](docs/plans/M4/M4-adoption.md) P2;
  [`docs/plans/M4/M4-step0-spike.md`](docs/plans/M4/M4-step0-spike.md) A-5.

## 5. The audit log

- **It isn't tamper-proof.** It records what agents ran, where the sandboxed code can't reach it.
  You, and any tool running as you, can edit it. An agent's own shell tool is one such tool.
  `tests/test_audit_acceptance.py::test_c5_native_cant_see_the_audit_dir`,
  [`docs/plans/M3/M3-audit-usage.md`](docs/plans/M3/M3-audit-usage.md) A12
- **Who can read it.** On Linux and macOS each file is 0600, and a folder Vestibule creates is
  0700. A folder that already exists keeps its own mode. On Windows the files take their
  folder's permissions: the default folder under your profile is limited to you, SYSTEM and
  Administrators, and a custom one may let other users read it.
  `tests/test_audit.py::test_the_dir_it_makes_is_0700_and_the_file_0600`
- **What it keeps:** the code of each call, cut at 4 KiB, and the sizes of its output, never the
  output itself. [`docs/plans/M3/M3-audit-usage.md`](docs/plans/M3/M3-audit-usage.md) A2

## 6. The server's own error output

The server logs to stderr through a queue and its own writer thread, so a client that stops
reading can't block it. Four paths still write to stderr directly. Three are crash paths:
- Python's hooks for an exception in a thread, and for one it can't raise;
- the native helper process, only when it crashes;
- on Windows, an exception that escapes the server's main loop. If the client has stopped
  reading, the exit then waits for the client's kill.

The fourth is one line at an ordinary shutdown on Linux and macOS, written only after a check
that the pipe has room for it.

`tests/test_stderr_log.py::test_a_stuck_stderr_never_blocks_a_log_call`,
[`docs/plans/M3/M3-audit-usage.md`](docs/plans/M3/M3-audit-usage.md) A11; BUILD_LOG's 2026-11-05
row.

## 7. Reporting a vulnerability

Please report it privately, through GitHub:
<https://github.com/victorperson999/Vestibule/security/advisories/new>. Don't open a public issue
for it.

Include:
- your OS, and the `isolation:` line of an affected run;
- the Vestibule version (`uv tool list`, or `pip show vestibule-mcp`);
- the code or steps that show it, and what you expected.

**Supported versions:** 0.1.x.
