# M4 step 0 — Spike report

Date: 2026-11-08. Contract: [`M4-adoption.md`](M4-adoption.md) §6, "Step 0 measures".

**No repo code.** The wheel was built with `uv build --wheel` from a `git archive` export of
`cbf1152` (`src/` is unchanged since `7a3c30f`). It holds only `vestibule/` and its
`dist-info`. The spike's scripts and logs live outside the repo, in `C:\Users\victo\m4spike\`
(`logs\` holds every run's full output) and in WSL at `~/m4spike/`. Every install ran in
throwaway directories (`UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, `UV_CACHE_DIR`,
`UV_PYTHON_INSTALL_DIR`, and the `VESTIBULE_*` paths), so nothing reached the real uv, PATH
or `~/.vestibule`.

Hosts:
- **Windows:** Windows 11 Pro 10.0.26300, Windows PowerShell 5.1.26100.9818, uv 0.11.15
  (winget) and 0.13.7, Docker Desktop 28.5.1.
- **WSL:** Ubuntu 26.04.1, kernel 6.6.87.2, uv 0.13.7.
- **Containers:** `ubuntu:24.04` and `debian:trixie`.

## Verdict

**Part A, on this machine, is done, and no signed decision changes.** Every install worked on
every Python tried, and every first call reported the isolation expected. Three findings
change what the README must say:
- on Windows, the `uv tool update-shell` step;
- on Linux, the uninstall command;
- in `SECURITY.md`, the workspace's symlinks.

**Part B, on the VMs and the GUI clients, is done, and no signed decision reverses.** Nested
virtualization, the GUI clients' `PATH` and stock Ubuntu came out as predicted. Two predictions
were wrong, and six findings change the plans (B-1 to B-6). The amendments to the contract wait
for Victor's sign-off, so that §3 changes only once.

## Results, part A

| Question (§6) | Result |
|---|---|
| `uv tool install` from the wheel, Windows | **Works.** Fresh cache: 7.4 s, 31 packages, `mcp` 1.30.0, `anyio` 4.16.0. uv took the first `python` on `PATH`, the system 3.11.9. With `--managed-python` it downloaded CPython 3.14.5 (21.4 MiB) and took 12.5 s. Both: `initialize`, `tools/list` (`run_code`, `read_workspace`), and one `run_code` → `isolation: container` in 3.7–4.3 s, including the selection probe. `audit` read the record; `setup` printed its Linux-only line and exited 0; stdout held only JSON. |
| The same, Linux, native | **Works** on the WSL system Python 3.14.4 and on uv-managed 3.11.18, 3.12.16, 3.13.18 and 3.14.10: `isolation: native`, the first call in 0.22–0.32 s, including the 157 ms probe. Install 3.7–5.8 s, 30 packages. The first `setup` fetched and unpacked both trees in 11.7 s; a re-run takes ~0.6 s. |
| Ubuntu 24.04's and Debian 13's system Pythons | **Install and serve** on 3.12.3 and 3.13.5, in their distro containers as a non-root user. uv chose the system interpreter and needed neither `python3-venv` nor `ensurepip`, which neither image has. `setup` worked (16.5 s). A call in a default container is `Blocked:` (Docker's seccomp refuses `unshare`), with the Docker fix. With Docker's seccomp, AppArmor and masked paths relaxed, the same interpreters ran the sandbox: `native-degraded (limits not applied: cpu, memory, pids — no systemd user bus)`, since a container has no user bus. |
| Does uninstall follow a directory link? Windows | **No.** Windows PowerShell 5.1's `Remove-Item -Recurse -Force` and `cmd /c rmdir /s /q` removed the folder and left a sentinel intact. That held for a host-made absolute symlink, relative symlink and junction, and for links a guest made through Docker Desktop. `Remove-Item` on a link removes only the link. (.NET's `Directory.Delete(path, true)` stops at a junction with "access denied"; nothing should use it.) Review finding 11 is refuted on this build. |
| The same, Linux | **`rm -rf` doesn't follow links, but plain `rm -rf` can fail** (finding A-2). |
| Debian 13's user-bus packages (from packaging, not an install) | `dbus-user-session` is optional, and recommended by `libpam-systemd`, which is standard and recommended by `systemd-sysv` (important). `dbus-user-session` depends on `libpam-systemd`. `openssh-server` recommends `libpam-systemd` too. |
| What `uv tool update-shell` does on Windows (uv's source) | It writes `HKCU\Environment\Path`, then broadcasts `WM_SETTINGCHANGE "Environment"`. |

## Findings for the step plans (none reverses a decision)

- **A-1 (step 4, the README) — on Windows, the tool isn't on `PATH` after
  `uv tool install`.**
  - uv warns that `%USERPROFILE%\.local\bin` "is not on your PATH" and names
    `uv tool update-shell`.
  - winget's uv, which this machine has, doesn't add that folder. uv's own installer puts uv
    itself there, so it probably does; part B checks that on the clean VM.
  - So §3's Windows commands need `uv tool update-shell`, and a new terminal, before
    `claude mcp add`. GUI clients get the folder from Explorer after the broadcast; part B
    measures that.
- **A-2 (step 4's uninstall; criterion 9) — a guest can lock folders that plain `rm -rf`
  can't remove.**
  - The guest runs with the user's uid (N6), so `chmod 000` or `chmod 500` on a workspace
    folder works.
  - `rm -rf` then fails with "Permission denied" and leaves the tree.
  - `chmod -R u+rwX ~/.vestibule && rm -rf ~/.vestibule` removed everything, and didn't follow
    the guest's links: a link's target kept its mode 500 and its files.
  - The unpacked trees have no folder without owner write, so only the workspace needs this.
  - On Windows, the guest's `chmod` is Linux metadata only. `chmod 400` shows as ReadOnly, and
    `Remove-Item -Force` clears it.
  - Criterion 9's test should include a locked folder.
- **A-3 (step 4, `SECURITY.md`) — the workspace's symlinks are the guest's, and on the host
  they can point anywhere.**
  - Through Docker Desktop with Developer Mode on (this machine), a guest's relative link
    became a real NTFS symlink (`0xA000000C`) when its target exists on the host, even though
    the guest can't see that target.
  - `../../../sentinel` pointed outside the workspace. The host read a sentinel file through
    it, while the guest got "No such file".
  - Absolute targets, and relative ones that don't exist on the host, became WSL's own link
    type (`0xA000001D`), which Windows doesn't follow.
  - On Linux every link is real.
  - This isn't an escape:
    - inside the sandbox, the link resolves in the sandbox's own view;
    - `read_workspace` refuses links;
    - the deletes above don't follow them.
  - But a host tool that follows links (Explorer, an editor, a backup, the agent's own shell)
    would follow them out of the workspace, so F5 §2 should say so.
  - Without Developer Mode, the links are expected to be WSL links. That's untested, because
    turning Developer Mode off needs admin.
- **A-4 (step 3, F1's classifiers and the gate) — users will meet 3.11 to 3.14.**
  - uv uses the first interpreter on `PATH` (Windows), or the system `python3` (Linux).
  - With neither, it downloads the newest managed CPython: 3.14 today.
  - The gates run 3.11.9 (Windows) and 3.11.17 and 3.14.4 (WSL); 3.12 and 3.13 ran only this
    spike's smoke test.
  - Step 3's smoke script can loop `--python 3.11 … 3.14` cheaply. F1's classifiers claim
    what ran.
- **A-5 (step 4, F5 §4) — the package count.** It's 30 installed packages on Linux and 31
  on Windows (`pywin32`), counting `vestibule-mcp` and `mcp`, not "about 25". Today's resolve
  matches §7: `mcp` 1.30.0, `anyio` 4.16.0, `pydantic` 2.14.3.

## Results, part B

Run 2026-11-08 from the same wheel, with uv 0.13.7 on the VMs. The full output is in `logs\`:
`vm-*.txt`, `winvm-result.txt` and `gui-path-*.txt`.

Hosts:
- **`m4-ubuntu`:** Ubuntu 24.04.5, desktop defaults, kernel 7.0.0-42, 2 CPUs, 4 GB.
- **`m4-debian`:** Debian 13.7 netinst, no tasks, `openssh-server` with
  `--no-install-recommends`, kernel 6.12.115, 2 CPUs, 2 GB.
- **`m4-win11`:** Windows 11 Pro build 26300, 4 CPUs, 8 GB, nested on the Ryzen 7600X host.
- **GUI clients, on this machine:** Claude Desktop 4.23118 (the Store package), Cursor 3.29.9.

| Question (§6) | Predicted | Result |
|---|---|---|
| Claude Desktop's and Cursor's `PATH` on Windows | Both see `.local\bin` without a re-login, and `docker` | **As predicted.** Both were opened from the Start menu after `uv tool update-shell`, with no sign-out. In both, `where` found `.local\bin\vestibule-mcp.exe` and `docker.exe`. `update-shell` prepended the folder to the user `PATH`; the machine `PATH` doesn't have it, and Explorer was already running. Cursor started the bare-name `vestibule` entry and connected to it. Claude Desktop left no server log to read, so its own start of the server is unconfirmed; P6's live session covers that. |
| Nested virtualization running Docker Desktop | Works on this host | **Works.** Docker Desktop 4.98.0 (engine 29.9.2) on WSL 2. One `run_code` → `isolation: container` in 1.8 s, including the probe. Pulling both pinned images took 25 s. Installing Docker Desktop took extra steps (B-3). |
| Stock Ubuntu 24.04, no Docker | The helper's id-map write fails, and `_native_hint` names Ubuntu | **As predicted.** `apparmor_restrict_unprivileged_userns = 1`. `Blocked:` with `[Errno 13] Permission denied: '/proc/self/setgroups'` and "Ubuntu restricts unprivileged user namespaces on this host". Install 3.4 s on the system 3.12.3. `setup` still fetched the trees (7.4 s), which native can't use there. |
| Snap Docker | `no-new-privileges`, and no access to `~/.vestibule` | **Half right** (B-5). Docker 29.8.0: `Blocked:` with `probe exit 255 … exec /sbin/docker-init: operation not permitted`. |
| Docker Engine from Docker's apt repo | `container` | **As predicted.** Docker 29.9.3: `isolation: container`, first call 0.74 s, with the default `~/.vestibule/workspace` as well. No container left behind. |
| Debian 13 minimal's user bus | No bus, so `native-degraded (… no systemd user bus)`; after `dbus-user-session` and a new login, `native` | **No bus, as predicted; the fix needs a reboot** (B-2). `setup` failed first, for a reason nobody predicted (B-1). No system Python, so uv fetched 3.15.2 (B-4). After `ca-certificates`: `native-degraded`. After `dbus-user-session` and a new login: still `native-degraded`. After a reboot: `native`, first call 0.13 s. |

## Findings from part B, for the step plans

- **B-1 (step 4, the README; §3's Linux row) — on a minimal Debian, `setup` fails until
  `ca-certificates` is installed.**
  - `uv tool install` works because uv brings its own roots, so `setup` is the first thing to
    fail: `failed: docker.io: [SSL: CERTIFICATE_VERIFY_FAILED] … unable to get local issuer
    certificate`, exit 1.
  - `registry.py` uses `urllib`, so Python's default trust store, and `/etc/ssl/certs` was
    empty. Installing `ca-certificates` fixed it.
  - The message doesn't name the fix. The options are a prerequisite line, a hint in `setup`'s
    message, or `certifi`'s bundle (already in the tree through `httpx`, but it would become a
    direct dependency).
- **B-2 (step 4; §3's Linux row; P6) — Debian's user-bus fix needs a reboot.**
  - `dbus-user-session` installs, and pulls in `libpam-systemd` and `dbus`, but
    `dbus.socket`, `dbus.service` and `systemd-logind.service` stay inactive. A new login still
    has no `XDG_RUNTIME_DIR`.
  - After a reboot all three are active and the run reports `native`.
  - Starting the services by hand wasn't tried; a reboot is what was measured.
- **B-3 (step 4; §3's Windows row) — Docker Desktop's recommended install fails on a clean
  Windows 11.**
  - The installer's default, "Per-user installation (Recommended)" with WSL 2, stops: it needs
    the "Virtual Machine Platform" feature and can't enable it without admin.
  - After `wsl --install --no-distribution` the installer finished, but Docker Desktop still
    said the feature was off. (The installer's own hint is plain `wsl --install`, not tried.)
  - `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` in an
    admin PowerShell, then a restart, fixed it. One sample.
- **B-4 (step 3's smoke loop and F1's classifiers; updates A-4) — uv 0.13.7 picks Python 3.15.**
  - With no usable system Python, uv fetched CPython 3.15.2. That was Debian minimal, and
    Windows with only the Store's `python` stub. A-4 said 3.14.
  - Both runs passed: `native` on Debian, `container` on Windows. No gate runs 3.15, so step
    3's loop should include it, and F1's classifiers claim what ran.
- **B-5 (P3's wording; step 4's Ubuntu notes) — the snap's real blocker, and its message.**
  - Direct `docker run`s on the snap: plain and `--init` work, and
    `--security-opt no-new-privileges` alone fails with `exec … operation not permitted`.
    Vestibule always passes that flag.
  - P3's other reason is half wrong on this snap: a bind mount of `~/.vestibule/workspace`
    worked. A bind mount of a host `/tmp` folder came up empty, and run dirs live there, so
    the snap would likely still fail without that flag.
  - Vestibule's message says "the container runtime cannot enforce the isolation profile" and
    doesn't name the snap. A user following Ubuntu's own suggestion gets no pointer to Docker's
    apt repo.
- **B-6 (P1's wording) — these two GUI clients don't need the absolute path.**
  - P1 has the README give GUI clients the path from `uv tool dir --bin`. Bare
    `vestibule-mcp` resolved in both, when the client started after `update-shell`.
  - A client left running through `update-shell` wasn't tested.

The VMs stay for P6's first-run pass. `m4-ubuntu` and `m4-debian` have a `fresh-install`
checkpoint from before the kit's key and `sudo` rule. `m4-win11` has none, and now holds Docker
Desktop.
