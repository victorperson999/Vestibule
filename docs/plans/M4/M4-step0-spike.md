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

**Part B needs the VMs and the GUI clients, and is pending.** It covers the GUI clients'
`PATH`, nested virtualization, stock Ubuntu 24.04, and Debian 13 minimal. The amendments to the
contract are applied once part B is in, so that §3 changes only once.

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

## Part B — pending (needs Victor), with what the code predicts

| Question | How | Predicted |
|---|---|---|
| Claude Desktop's and Cursor's `PATH` on Windows | Install both. `gui\setup.ps1` does the README's install and `update-shell`, then adds a `PATH` recorder and a bare-name `vestibule` entry to each config. Open and quit each client from the Start menu; `gui\restore.ps1` puts the configs back. | Both see `.local\bin` without a re-login (the broadcast), and `docker` from the machine `PATH`. |
| A Hyper-V VM with nested virtualization running Docker Desktop | `vm\create-vms.ps1 -Win11Iso …`: TPM, `ExposeVirtualizationExtensions`, static 8 GB. Inside, Docker Desktop, then `winvm-kit\check.ps1`. | Works on this Ryzen 7600X host; if not, P6's fallback user account. |
| Stock Ubuntu 24.04's failure stage and message; snap Docker's | `m4-ubuntu` VM, desktop defaults. Claude runs the kit over SSH (`vm\authorize.ps1`), then installs the Docker snap, then Docker's apt repo. | The helper's id-map write fails, and `_native_hint` adds "Ubuntu restricts unprivileged user namespaces on this host…" (`select.py:673`). Snap: `no-new-privileges`, and no access to `~/.vestibule`. Apt Docker: `container`. |
| Debian 13 minimal's user bus | `m4-debian` VM: no tasks in tasksel, root password left empty (so `sudo` is set up), then `openssh-server` with `--no-install-recommends`, so the bus packages don't come with it. | No `libpam-systemd` and no `dbus-user-session`, so `native-degraded (… no systemd user bus)`. After `apt install dbus-user-session` and a new login, `native`. |
