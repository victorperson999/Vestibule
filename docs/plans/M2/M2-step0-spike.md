# M2 step 0 — Spike report

Date: 2026-10-14. Host: WSL2 Ubuntu 26.04.1, kernel 6.6.87.2, libseccomp 2.6.0 (API level 7),
Docker Desktop 28.5.1. Contract: [`M2-native-warden.md`](M2-native-warden.md) §10 step 0.

**No repo code.** The spike scripts and every log are kept in WSL at `~/m2spike/scripts/`
and `~/m2spike/logs/`. The scripts are `warden.py`, the helper; `sc.py`, the libseccomp
binding; and `server.py`, the harness. The rootfs trees (`~/m2spike/rootfs-{python,node}`)
came from `docker export` of M1's two pinned digests, which is acceptable for the spike only
(N2). The seccomp profile is moby's `seccomp/default.json`, now in the **`moby/profiles`**
repo at commit `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef` (2026-09-17). It's no longer in
`moby/moby`; that path now returns 404.

## Verdict

**Every kernel fact N1–N5 rest on holds on this host, so no signed-off decision changes.**
The spike did find six things the contract has wrong in detail. The amendments below fix
them; none reverses a decision. It also leaves two questions and a benchmark finding for the
step plans.

## Results

| Question (contract §10 step 0) | Result |
|---|---|
| §3's chain, unprivileged | **Works** for python, bash and node. One correction, A1. |
| What the guest sees | uid/gid 1000; `CapInh/Prm/Eff/Bnd/Amb` all 0; `NoNewPrivs 1`; `Seccomp 2`; PIDs `[1, 2]`; only `lo`; no `/sys`; hostname `vestibule`; cwd `/workspace`; Python 3.12.15, Node v22.23.3. |
| Mounts inside | `/` and `/sandbox` are `ro,nosuid,nodev`; `/proc`, `/dev` (tmpfs + bound nodes), `/dev/pts` (devpts newinstance), `/dev/shm` and `/tmp` (tmpfs, 64 MiB) are present. `/workspace` is `rw` with **no** `nosuid,nodev` (A3). |
| PID-1 hardening (the review's `/proc/1` attack) | `/proc/1/{environ,cwd,root,fd,mem}` are all `EACCES`. `/proc/1/cmdline` still showed the host path until the A4 fix. |
| Writes | `/` and `/sandbox` give `EROFS`; `/tmp` and `/workspace` work. |
| Network | TCP and UDP give `ENETUNREACH`; DNS fails; `AF_VSOCK` and `AF_PACKET` give `EPERM`. |
| Parity (N9) | `openpty` works, `multiprocessing.Lock` (sem_open in `/dev/shm`) works, and `subprocess.run(['python', …])` resolves via the image's `PATH`. |
| libseccomp via ctypes | Moby's profile builds to 309 rules in ~1 ms (32-bit-only names are skipped as unknown on x86_64). The filter loads in init, and the guest inherits it. |
| Notification and fd hand-off | Init sends the listener fd to the helper over a socketpair (`SCM_RIGHTS`). `unshare`, `mount`, `ptrace`, `keyctl`, `io_uring_setup`, `bpf` and `socket(AF_VSOCK)` all return `EPERM` and are reported by name. |
| Notification storm (50 threads looping `mount` for 2 s) | With the cap at 256, reporting stopped at 256 with names deduplicated (`["mount"]`). Uncapped, the helper answered 66,980 notifications in 2 s (~33k/s). |
| Per-run scope (N3 (i), `busctl` + paused helper) | Works. Limits applied and read back exactly (`memory.max 268435456`, `memory.swap.max 0`, `pids.max 128`, `cpu.max 75000 100000`). `busctl` takes 10–50 ms; the cgroup is ready ~50 ms after spawn (median), overlapping the helper's own start. |
| `systemd-run --scope` wrapper (another way to do (i)) | Works the same, but slower (B1). |
| Move the server once (N3 (ii)) | Works. One-time move 24.5 ms; per-run cgroup setup 0.9 ms. |
| Memory bomb | Guest SIGKILLed; `oom_kill 1`; `memory.peak` 256 MiB. Works in (i) and (ii). |
| Fork bomb | `EAGAIN` after 126 forks (128 = init + guest + 126). |
| Timeout: busy loop and self-`SIGSTOP` | `cgroup.kill` on the `supervisor/` and `guest/` leaves ends both in ~2 ms; nothing is left in the cgroup. |
| Server hard-killed (`SIGKILL`) mid-run | No helper, init or guest survived; all gone within 12 ms, with and without cgroups (PDEATHSIG chain). |
| Bus under the MCP SDK's default environment | `busctl --user` fails ("`$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined`"). With a derived `XDG_RUNTIME_DIR=/run/user/1000` it works; `/run/user/1000/bus` is a socket owned by the uid. |
| `RLIMIT_NPROC` per user namespace | Yes. With `RLIMIT_NPROC=20` the sandbox stopped at 20 processes counted inside its own user namespace (helper + init + guest + 17), while 9 other processes of the same uid ran on the host. The no-cgroup backstop works (N3). |
| Helper cost | Interpreter start ~8 ms; filter build ~1 ms; namespaces, mounts, `pivot_root` and the filter load ~5 ms. RSS ~14.5 MB. Memory a process allocated before it was moved stays charged to its old cgroup, so in (i) the scope sees only ~1.3 MB of the helper. |
| Abort before `execve` (N5) | Seen by accident: an import that failed after `pivot_root` made init report `{"init_error": "caps", …}` and exit, and the guest never ran. That is the behaviour N5 requires. |

### Cold start (python `print(1)`, end-to-end from spawn to result, after a warm-up run)

| Path | n | median | p95 |
|---|---|---|---|
| Native, no cgroup | 30 | 54.8 ms | 59.5 ms |
| Native, per-run scope via `busctl` (N3 (i)) | 30 | 75.4 ms | 121.6 ms |
| Native, `systemd-run --scope` wrapper | 30 | 96.0 ms | 139.7 ms |
| Native, server moved once (N3 (ii)) | 30 | 51.3 ms | 74.8 ms |
| Bare `docker run --rm --network none` (WSL CLI → Docker Desktop) | 10 | 386.9 ms | 410.0 ms |

**N3's rule picks (i).** It adds ~21 ms to the no-cgroup median (~24 ms over (ii)), under the
signed 50 ms threshold. Its p95 tail (122 ms) comes from the `busctl` process and systemd's
job queue. The step-4 plan may cut it with a direct D-Bus call; that's an optimization, not a
requirement.

## Amendments to the contract (applied as blockquotes in `M2-native-warden.md`)

- **A1 — The helper creates only the user and PID namespaces; init creates the mount,
  network, UTS, IPC and cgroup namespaces.** First version: the helper unshared everything,
  so init's `pivot_root` ran in a mount namespace the helper shared. `pivot_root` moves the
  root of *every* process in that namespace, so it pulled the helper into the sandbox root
  too. The helper then lost the host cgroup files and its own `/proc`. With the split, the
  helper keeps the host view, and the guest's view is unchanged.
- **A2 — In the per-run scope, the helper reads the run's stats.** systemd removes an empty
  transient scope, with our sub-cgroups, as soon as its last process exits. That means no
  `rmdir` is needed, but `memory.events` and `memory.peak` must be read before the scope
  empties. The helper reads them after init exits and puts them in the status document.
- **A3 — `/workspace` is bind-mounted `nosuid,nodev`.** The spike showed `rw,relatime` only.
  `no_new_privs` already blocks setuid elevation, so this is hygiene. `noexec` is not added,
  because guests run scripts from the workspace.
- **A4 — The helper is self-contained stdlib code, fed to the interpreter through a pipe,
  with a neutral `argv[0]`.** `vestibule-warden -I -c "import os;exec(os.read(3,…))"` makes
  `/proc/1/cmdline` show no host path, for both the system and a venv interpreter. As a
  result `sys.executable` is empty and the venv's site-packages aren't on the path, so the
  helper and the seccomp binding must not import anything outside the stdlib.
- **A5 — All imports happen before `pivot_root`.** After it, the host's Python stdlib is out
  of reach.
- **A6 — The rootfs completion marker lives outside the tree.** The spike's `.complete`
  marker, and `docker export`'s `.dockerenv`, showed up in the guest's `/`.

## Questions for the step plans (not reversals)

- **Q1 (step 5) — `io_uring` must be denied silently.** Node 22 probes `io_uring_setup` at
  startup: 4 notifications per run. Under N4's reporting subset, every node result would say
  `blocked syscalls: io_uring_setup` for innocent code. Docker denies it silently too.
  Proposal: take `io_uring_*` out of the notified subset (still denied).
- **Q2 (step 5) — `ptrace`: follow Docker or stay stricter.** Moby's profile *allows*
  `ptrace` and `process_vm_readv/writev` on kernels ≥ 4.8. The contract's subset denies and
  reports them, and §9 criterion 9 tests `ptrace`. With PID 1 non-dumpable and outside
  reach, `ptrace` inside the sandbox only touches the guest's own processes. The step-5 plan
  brings this to Victor: keep denying (stricter than Docker; breaks debuggers and `strace`
  in the sandbox) or follow Docker (amend criterion 9 to other syscalls).
- **B1 (step 7) — The benchmark won't show 10–50×.** On this host native is **~5×** faster
  than a bare `docker run` with the per-run scope, and ~7.5× without cgroups or with (ii).
  M1's real run path also does an image preflight and more CLI calls, so the gap to M1 is
  larger; step 7 measures it through `server.call_tool`. The README reports measured numbers
  only; PLAN.md's "10–50×" was an expectation, not a measurement.

## Not covered (left to the steps, as planned)

- Ubuntu's AppArmor restriction: impossible on this kernel (N5: injection in M2, a VM in M4).
- The real registry fetcher and unpacker (step 2).
- A busy loop staying near `cpu_pct`: only `cpu.max`'s readback was checked.
- The read-only workspace mode.
- The helper's CPU inside the envelope during a storm: only the notification cap was
  checked.
- An end-to-end server launch under the minimal environment: bus discovery was verified at
  the `busctl` level only (criterion 17 covers it).
- Test note: listing `/proc/self/fd` shows the listing's own fd (`[0,1,2,3]`). Criterion 10's
  test must account for it.
