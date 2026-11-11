# M4 step 4 — the README, `SECURITY.md` and the demo (F4, F5, P7, P8)

Status: **v2, signed off 2026-11-09 with option (1) on each of V1–V4. 4a built 2026-11-09 (§13 is
its as-built); 4b, the demo, built 2026-11-10 (§14).** v1 was reviewed by `skeptic` (24 findings: 2 high, 13 medium, 9
low; verdict "sound with amendments"). `verifier` checked 19 claims about the code, tools and logs:
all 19 confirmed, five with a correction (§12). Every finding is taken below except one part of
finding 5, with the reason given.

Contract: [`M4-adoption.md`](M4-adoption.md), F4, F5, P7, P8, §3, and step 0's findings A-1 to A-5
and B-1 to B-6 ([`M4-step0-spike.md`](M4-step0-spike.md)). Acceptance criteria 5, 6, 7 and 11 are
this step's. Criterion 9 (uninstall) and criterion 2's "only the README" are judged on the VMs in
step 5, which follows what this step writes. **Nothing is published here, and no `src/` changes in
this step.** V4's fix is a small step of its own, with its own plan. It lands first, so that
`SECURITY.md` can say what holds.

## 1. The problem

The README a stranger would land on today can't be followed, and parts of it aren't true for most
of its readers:
- **It installs from a clone.** "Not on PyPI yet", a venv, and `.venv/bin/…` paths. P1 is
  `uv tool install vestibule-mcp`.
- **It never says what isn't sandboxed.** An agent's own shell and file tools run on the host. The
  diagram suggests everything passes through the chamber.
- **It claims more than it can deliver** (§2.2). The headline says nothing is sent to a cloud
  service, while an agent on a hosted model sends the code and its output to that model's provider.
- **Its links are relative,** and it becomes the PyPI page, where they break.
- **There's no `SECURITY.md`,** and it points at one twice. Nine earlier plans carried items here,
  and two M4 plans add more.
- **Windows and Ubuntu need steps** that only step 0's VMs found.

## 2. The README (F4)

### 2.1 The new order

| # | Section | Comes from |
|---|---|---|
| — | Title, the lead (§2.2), badges, the GIF and caption (§6) | today's head |
| 1 | The analogy, the diagram, **Why use Vestibule?** with "What it is" folded in, and one short early-development callout (F1 says it stays) | today's, corrected by §2.2 |
| 2 | **What is and isn't sandboxed** (§2.3) | new |
| 3 | **Quick start,** per platform (§2.4) | new, replaces One-time setup |
| 4 | **Connect a client** with the three configs (§2.5), then **First-run troubleshooting** (§2.6) | new |
| 5 | **What it protects, and what it doesn't** (§2.7) | "Know the limits", cut down |
| — | **Uninstall** (§2.8) | new |
| 6 | The reference sections: **What `setup` does** (today's One-time setup bullets, rewritten per §2.2, and the two `docker pull` lines, which `test_pins.py` and `RELEASE.md` step 2 need), **Configuration**, **Native isolation on Linux**, **Usage numbers and the audit log**, **How it works** (per §2.2), **Status** (the milestone table, M4's row saying "in progress"), **Documentation**, **License** | today's |

Clone and dev install leave the README: GETTING_STARTED already has them.

**"Where native works" gets what was measured,** with its conditions:
- Stock Ubuntu 24.04: measured (step 0 part B, step 2's live run).
- Debian 13: measured on a netinst with no tasks and `--no-install-recommends`. As shipped, `setup`
  failed, then the run was `native-degraded`; it was `native` after `ca-certificates`,
  `dbus-user-session` and a reboot.
- Debian 12, Fedora and Arch stay "expected".
- The sentence "M4's fresh-machine test runs on a stock Ubuntu 24.04 VM" goes.

**Podman's caveat** stays beside `VESTIBULE_RUNTIME` in the Configuration table, because that row
offers Podman.

### 2.2 Claims that change meaning

| Today | Why it can't stay | Becomes |
|---|---|---|
| `:3` lead: "… for free, with nothing sent to a cloud service." | An agent on a hosted model sends what it wrote, and what a run printed, to that model's provider. | "Run untrusted AI-agent code in a sandbox on your own machine: free, and with no hosted sandbox service in the loop." (V2) |
| `:9`: "… runs *inside* the chamber, never touching the real system." | The workspace is a real host folder, and §2.3 says the agent's own tools run on the host. | "… the code an agent sends to Vestibule runs *inside* the chamber, not on your real system. Its one opening is a workspace folder." |
| `:30`, `:32`: code and data "ship to a third party's infrastructure", and Vestibule refuses "all three" | The new bullet admits a model provider still gets them. | The third cost reads "adds one more third party holding your code and data". "All three" is then true. |
| "**No cloud** — your agent's code and the data it touches never leave your machine. There is no third party to trust, to breach, or to leak." | The same. | "**No sandbox vendor** — Vestibule itself sends nothing anywhere: no account, no telemetry. The only downloads are `setup`'s, from Docker Hub. (Your agent still sends its code, and whatever a run prints, to its model's provider, as it would anyway.)" Checked: `registry.py` is the only network code in `src/`. |
| "works offline, on a plane, behind a corporate firewall." | True of the sandbox, not of an agent on a hosted model. | "The sandbox needs no network after `setup`." |
| Diagram: "no way through", "— untouched" | Golden rule 5 and "don't oversell": it shares the host's kernel. | "no direct path" and "— kept out" (same widths). One line under the diagram: only code sent to `run_code` goes through the chamber, linking §2.3. |
| `:185`, `:198`: "structurally impossible", "ships as `SECURITY.md` in M4" | §3 rejects the phrase, and the README is frozen into the package. | The same sentence as `SECURITY.md` §1 (§3 below), linking the file. |
| `:84` "normally `isolation: native` on Linux" | Not on stock Ubuntu. | Moves to each platform's expected line. |
| `:87` `.venv/bin/vestibule-mcp audit` | The dev install left. | `vestibule-mcp audit`. |
| `:89` "On Linux, `setup` unpacks about 357 MB" | Stock Ubuntu, and `VESTIBULE_BACKEND=container`, skip it. | "Where native is used, …" |
| "Not yet published to PyPI." and "Not on PyPI yet" | The README is frozen into the package at the upload. | Removed. |

### 2.3 What is and isn't sandboxed

States, in this order:
- Only code sent to `run_code` is sandboxed. The agent's own shell and file tools still run on your
  machine, and an agent that has them can use them instead.
- The sandbox-only session, which makes `run_code` the only way to run code:

  ```bash
  claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
  ```

  ```powershell
  # Windows PowerShell drops an empty "" argument, so go through cmd:
  cmd /c --% claude --tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace
  ```
- **What that line doesn't cover:** any other MCP server you've added still runs on your machine.
  `claude mcp list` shows them. `--strict-mcp-config --mcp-config <file>`, with a file naming only
  Vestibule, leaves them out, and `--permission-mode dontAsk` refuses a tool that isn't allowed
  instead of asking you. The demo used both.
- Other clients have their own settings for this, and only Claude Code's flags were tried.

**The `cmd /c --%` form:** measured here on PowerShell 5.1 with a stand-in `claude.cmd`. The plain
form loses the empty argument; `cmd /c --%` keeps it, whichever of `.exe` or `.cmd` an install
provides, and it isn't affected by PowerShell 7.3's change to argument passing. A real session
confirms it (§7, C). **Why not `--restricted`** (Claude Code's newer flag, which needs no empty
argument): it leaves the file tools, confined to the working directories, and ignores the user's
settings files. F4's line and both kits use `--tools ""`, which removes every built-in tool.

### 2.4 The quick start

Each platform: its prerequisites, its commands, and the line to expect. All of them end the same
way, with "ask your agent to run `print(1)`" and the isolation that result should show. What each
line rests on:

| Line | Where it was measured | Left for step 5 |
|---|---|---|
| uv's own installer, with a link to uv's other install methods, **then a new terminal on every platform** | uv's installers read today. On Windows `Add-Path` writes the user's `PATH` and never the current session's, so `uv` isn't found in the same terminal | running it (A-1's carry) |
| Ubuntu and a minimal Debian: `curl` first. It's `sudo apt install curl` on Ubuntu, and part of Debian's line below | `curl` is absent on stock Ubuntu and on Debian minimal (the VM logs) | the VMs |
| `uv tool install vestibule-mcp` | step 0 from a wheel; step 3's smoke on 3.11–3.15 | from the index |
| Windows: `uv tool update-shell`, and "if it says it changed your `PATH`, open a new terminal" | A-1, B-6. It's a no-op where uv's installer already did this, and winget's uv needs it | |
| `vestibule-mcp setup` | step 2's live run; step 3's smoke | |
| `claude mcp add --scope user vestibule -- vestibule-mcp` | **The bare name has never been used in a live Claude Code session** (M2 and M3 used absolute paths) | settled in §7, C |
| Debian minimal: `sudo apt install ca-certificates curl dbus-user-session`, then a reboot. "No `sudo`? Run it as root with `su -`" | B-1, B-2 (the `sudo` case isn't measured: that VM had it) | the Debian VM |
| Ubuntu: Docker Engine from Docker's apt repo, by **a link to Docker's page**, then `sudo usermod -aG docker $USER` and a new login | part B's apt run, 29.9.3 | the Ubuntu VM |
| Windows: Docker Desktop, by a link to Docker's page. **Worded as a fix for the failure measured, not a first step:** "If its recommended install stops and asks for the Virtual Machine Platform feature, this fixed it on our one test machine: `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All` in an admin PowerShell, then a restart" | B-3, one sample, in that order | not re-run: P6's clock starts with Docker installed, and no clean Windows VM is left |
| macOS | **Says untested for runs.** Its wording follows the manual job's real state when this commits (§7, A) | |

The Ubuntu section also says: why native isn't used there (the AppArmor restriction); that the snap
can't run Vestibule, because `no-new-privileges` stops its containers from starting; that the
`docker` group is root-equivalent; and that rootless Docker avoids that but hasn't been tried with
Vestibule. **Docker's install stays a link,** because Docker's page is maintained and a copy goes
stale, and P6's clock starts with Docker already installed.

The Windows section also says: for native isolation, run the agent and Vestibule both inside WSL2
with systemd on. A Windows client driving a server inside WSL isn't designed or tested (M2's E2).

### 2.5 The client configs

One table, each row saying what tested it, per F4 and criterion 5:

| Client | Config | Mark |
|---|---|---|
| Claude Code | the one command | tested, Linux and Windows, by §7, C's two sessions |
| Cursor | `~/.cursor/mcp.json`, bare `vestibule-mcp` | tested on Windows. **A failed Cursor session blocks this step:** criterion 5 requires it unless Victor amends the criterion |
| Claude Desktop | Settings → Developer → Edit Config, bare `vestibule-mcp` | tested on Windows if §7, C passes; otherwise "untested" (criterion 5 allows that) |
| Either GUI client on macOS | the absolute path from `uv tool dir --bin`, and `env.PATH` with Docker's directory | untested |

Below the table: a client must start after `uv tool update-shell` (B-6; a client left running
through it wasn't tested), and a client that can't find `docker` takes `env.PATH` (P5).

### 2.6 First-run troubleshooting

Only what a first run hits, each with its fix; everything else links GETTING_STARTED:
- `uv` or `vestibule-mcp` not found: a new terminal, then `uv tool update-shell`.
- `CERTIFICATE_VERIFY_FAILED` from `setup`: `ca-certificates` (B-1).
- `isolation: native-degraded (… no systemd user bus)`: `dbus-user-session` and a reboot (B-2).
- `Blocked:` naming AppArmor or the snap: the Ubuntu section.
- Docker's daemon not running, or the user not in the group: `setup`'s own message.
- The `server/discover` warning, "31 validation errors": harmless, and why (F4 names it).

### 2.7 What it protects, and what it doesn't

Four items that change how you use it stay in the README: not a VM; the workspace is writable and
its size isn't capped; treat the workspace like an untrusted download (symlinks); a run's output
goes back to your agent. Everything else in "Know the limits" **moves** to `SECURITY.md`, so each
fact has one home, and the section links there.

### 2.8 Uninstall (F4, criterion 9)

"Quit your client first", because a running server holds files open on Windows. Then:

```bash
claude mcp remove --scope user vestibule      # GUI clients: delete the entry from the config
uv tool uninstall vestibule-mcp
chmod -R u+rwX ~/.vestibule && rm -rf ~/.vestibule   # A-2: a guest can lock its own folders
docker image rm <the python ref> <the node ref>      # only if setup pulled them
```

```powershell
Remove-Item -Recurse -Force $HOME\.vestibule   # step 0: doesn't follow links
```

The `docker image rm` line carries the literal digests so it can be pasted, and §5 keeps them
current. **It warns first that `~/.vestibule` holds your workspace and audit log.** It names what
stays: uv and any Python it downloaded; a folder you set with `VESTIBULE_WORKSPACE`, `_ROOTFS_DIR`
or `_AUDIT_DIR`; anything named `vestibule-*` in the temp folder after a hard kill (root-owned ones
in `/tmp` need `sudo`); containers named by `docker ps -a --filter label=vestibule.run=1`.

### 2.9 Absolute URLs

Every link and image becomes `https://…`: files as `https://github.com/victorperson999/Vestibule/blob/main/<path>`,
folders as `tree/main/<path>`, and the page's own headings as
`https://github.com/victorperson999/Vestibule#<heading>`, because PyPI doesn't keep GitHub's heading
ids. Converting them would quietly lose `test_docs`'s check on those targets, so §5 puts it back.

## 3. `SECURITY.md` (F5, P8)

At the repo root, where GitHub lists it as the security policy. About 190 lines.

**Form.** Written for a stranger. Each claim ends with where it comes from, in a compact tail: a
test as `tests/test_x.py::test_y` or a whole file as `tests/test_x.py`, a plan as a relative link, or
a `docs/BUILD_LOG.md` row. §5's test keeps those real. M1 and M2 each have an `S7-D2`, so every
decision code carries its milestone.

| F5 § | What it says | Sources |
|---|---|---|
| 1. What it's for | The risks it's designed against, **for code sent to `run_code`**, and what it isn't (a VM, a defense against kernel bugs, a guard on an agent's own tools). **Wording changes from F5,** below | README; `ARCHITECTURE.md` "Honest scope" |
| 2. The boundary | Native's layers; seccomp from Docker's profile at moby `6fe7deb`, under Apache-2.0, with `ptrace` allowed and `AF_VSOCK` refused; the per-run verdict; the container profile, flag by flag; what `-degraded` means; `read_workspace`'s jail, **and its race (V4)**; `naive` and `none` | `test_native_acceptance.py` c02, c03, c04–c07, c09, c10; `test_native_seccomp.py::test_ptrace_policy_is_allow_and_io_uring_is_never_notified`; `test_native_verdict.py`; `test_acceptance.py` c02–c05, c10, c11 and `test_hard_and_soft_controls_apply_inside_the_guest`; `test_native.py::test_without_a_bus_the_run_degrades_and_the_backstop_holds`; `test_workspace.py`; `M2-step5-seccomp.md:362` for the attribution |
| 3. Known residuals | F5's eleven: guest symlinks, with A-3's Windows detail; no disk cap; `app.slice`; what a native guest can learn, with step 1's `/sandbox` path note; the x32 point; the container self-deadline; a container left Created; the Windows shutdown race; forced native without a user manager, **and `auto` reaching `native-degraded` when no container works**; a root server refused; the `docker` group. **Added:** the `read_workspace` race (M1 assigned it here; V4 decides its text); reaping and a remote daemon's clock | M1 S7-D2 (`M1-step7-acceptance.md:232`), M2 N4, A-3; BUILD_LOG's known-issue rows (S7-D5, 7a, Windows shutdown, disk cap); M2 S7-D2 (`M2-step7-acceptance.md:88`); `M2-step3-warden-core.md:634`; `M4-step1-run-dir-sweep.md:264`; `test_select.py::test_a_server_running_as_root_runs_no_code`; P3; `M1-container-backend.md:203`; `M1-step4-lifecycle.md:96`; `vm-debian-nobus` in step 0 |
| 4. Supply chain | Pinned images and the re-pin tradeoff; every downloaded byte checked; an unpinned override's warning; one direct dependency, the tested minor, and the rest resolved fresh at install, so what a user gets can differ from what the gates ran | M1 S6 (`M1-step6-pinning.md:157`); `test_pins.py`, `test_registry.py`, `test_rootfs.py`; `test_select.py::test_unpinned_override_warns_for_both_refs`; A-5's counts, re-measured at the build |
| 5. The audit log | Not tamper-proof; out of the guest's reach; the folder's permissions, Windows' inherited ACLs, a folder that already exists keeps its mode | M3 A12; `test_audit_acceptance.py::test_c5_native_cant_see_the_audit_dir`; `test_audit.py::test_the_dir_it_makes_is_0700_and_the_file_0600` |
| 6. Stderr's crash paths | The thread and unraisable-exception hooks, the helper's crash-only write, Windows' escaped exception | M3 A11 and its amendment; BUILD_LOG's row; `test_stderr_log.py::test_a_stuck_stderr_never_blocks_a_log_call` |
| 7. Reporting | GitHub's private reporting, its link, what to include; supported versions 0.1.x | P8 |

**Where this departs from F5's wording (needs your OK with the plan):**
- **"Structurally impossible" isn't used,** because F5's own risks aren't all out of reach. §1 says:
  for a run reported as `native` or `container`, the host's other files, the network, and the host's
  memory, CPU and processes past the caps are out of a run's reach, short of a kernel or runtime
  bug. It then names what stays possible: deleting or poisoning anything in the workspace; filling
  the disk through the workspace; printing what it can read back to the agent; the caps a `-degraded`
  run reports as missing; a host tool that follows a link the guest planted; and V4's race.
- **"About 25 transitive packages"** becomes the measured count (A-5: 30 on Linux, 31 on Windows,
  counting `vestibule-mcp` and `mcp`).
- **Two additions:** what hasn't been tested (arm64, runs on macOS, Podman beyond its check), and "no
  one independent of this project has reviewed or audited this code", which is true today.

**P8:** private vulnerability reporting is off today (GitHub's API says `"enabled": false`). The link
in §7 is dead until Victor turns it on (§7, B). The same API confirms it.

## 4. The other docs

- **GETTING_STARTED:** its two references to "One-time setup" point at Quick start. Nothing else.
- **ARCHITECTURE:** the heading "Honest scope (put this in SECURITY.md)" links the file, and `:233`
  drops "structurally impossible" for §3's sentence.
- **`CLAUDE.md:96`** has the same phrase in the posture summary. Left alone: that file is Victor's
  operating rules, and a published-claims pass doesn't reach it unless Victor says so.
- **`docs/RELEASE.md`:** step 2's image list gains the uninstall's `docker image rm` line. Step 3 gains
  one line: private reporting is on, and the README's tested/untested marks are true.
- **The contract,** dated amendments, all needing your OK with the plan:
  - P6: V1 moves the GUI sessions earlier.
  - Criterion 2: "every command typed is one the README shows" counts a page the README sends you to,
    for Docker's install alone.
  - P6's clock: Debian's packages, and the reboot they need, are prerequisites outside the clock, like
    Docker. They apply only to a minimal install.
  - §3's Windows row: the Virtual Machine Platform line is worded as the fix for the failure measured.
  - F5: §3's departures. P7: §6's details.
- **HISTORY and BUILD_LOG,** as usual.

## 5. Tests (`tests/test_docs.py`, `tests/test_pins.py`; both platforms, no skips)

1. `test_readme_links_are_absolute`: every `](…)` target starts with `https://`, and the README has
   no raw `<img>` or `<a>` and no reference-style link. Red today on `LICENSE` and every relative link.
2. `test_readme_links_into_this_repo_exist`: a `blob/main/`, `tree/main/` or raw `main/` URL names a
   path that exists, with the exact case, and a URL into this repo on any other branch fails.
3. `test_readme_heading_links_resolve`: every `…/Vestibule#slug` matches a real heading, by GitHub's
   slug rule. It's permanent because step 5's fix loop is when headings change.
4. `test_security_md_cites_only_what_exists`: every `tests/x.py::test_y` names a real test, every
   `tests/x.py` a real file, and there's at least one of each.
5. `SECURITY.md` joins `_scanned()`, in the same edit that creates the file (`_text` raises until it
   exists), and joins `test_scan_covers_the_live_docs_source_and_tests`.
6. `tests/test_pins.py`: the README's `docker image rm` line quotes both refs.
7. With the GIF: `test_demo_gif_is_under_3_mb`, which also checks it's a GIF.

**Proving they can fail,** each red in its named test after a green control, sources restored byte
for byte: a relative link put back; an `<img>`; a misspelled repo path; a `tree/` link on another
branch; a heading renamed; a cited test renamed; `SECURITY.md` dropped from `_scanned()`; a re-pin
that misses the `docker image rm` line; and, in 4b, a padded GIF.

## 6. The demo (P7)

Recorded in WSL, in the live-check kit's launch (`~/vestibule-live/start.sh`: only Vestibule's two
tools). Claude writes these, outside the repo, in `~/vestibule-live/demo/`:
- `plant.sh`: creates `~/vestibule-demo/fake_key.txt`, holding text that says it's fake. It's
  removed afterwards.
- `prompt.txt`: says plainly it's a sandbox demo with a planted fake key, and gives **both calls as
  fixed code**, as the kit's M3 prompt does, one at a time. Call 1 reads that absolute path. Call 2
  posts a fixed placeholder string to `http://203.0.113.10/` with a 3 s timeout, so it doesn't depend
  on call 1. After each, show the result text unchanged in a code block, because Claude Code folds
  long tool results and `isolation:` is the last line.
  - **An IP literal, from the range reserved for documentation (RFC 5737).** A hostname would fail
    at DNS first, with "Temporary failure in name resolution", not P7's `Network is unreachable`, and
    this address can't reach anyone even if the sandbox failed.
- `record.sh`: shows the key file on the host, then starts the session under the recorder.

**The fallback** is P7's: if the model declines, the same two calls sit in a workspace file it's asked
to follow.

**What's on screen is checked against the record.** The result text the agent echoes is typed by the
model, so before the GIF is kept, each echoed result is compared with the tool's own result in the
session's transcript, as M3 5b judged its calls. The caption says the agent shows each result.

**Recording (V3):** `asciinema` writes the terminal's real output to a file, and `agg` renders it to
a GIF. No frame is drawn by hand. The only edits allowed are cutting the opening banner, shortening
pauses with an explicit `agg --idle-time-limit` (its default of 5 s would cut them silently), and one
uniform speed-up to reach about 20 s. **The caption says "sped up, pauses shortened".**

**Before it's committed, every frame is checked for anything private.** Claude Code's banner can show
the account's name or email, and `CLAUDE_CODE_HIDE_ACCOUNT_INFO` isn't in this version (searched its
binary). Claude extracts frames and reads them, and Victor looks too.

**The caption,** per P7: a real session where `run_code` was the agent's only way to run code; the
agent shows each result; sped up, pauses shortened; native is Linux's, and Windows, macOS and stock
Ubuntu get `container`.

**It's linked** by `https://raw.githubusercontent.com/victorperson999/Vestibule/main/docs/demo.gif`,
so it shows on PyPI once it's on `main`. Criterion 7's "renders on the PyPI page" can only be judged
after the upload, in step 5.

## 7. What Victor does, in order

Each gets exact "type this in that window" steps when its turn comes.

- **A. The macOS job, any time now.** The step 3 work is already on `main` (PR #29). Actions → macOS
  check → Run workflow. It has 0 runs, and the README's macOS sentence states what it finds. A failure
  there is a step 3 fix, not a README problem.
- **B. Turn on private vulnerability reporting:** repo Settings → Security → Private vulnerability
  reporting → Enable.
- **C. Live sessions (V1),** after the README's config blocks exist, pasted from the README, with a
  wheel of this commit:
  - **Before:** `uv tool uninstall vestibule-mcp` (step 0's v0.0.1 is still installed), then install
    the wheel. Sessions start from an **empty folder**, because a local-scope `vestibule` entry for the
    repo points at the dev venv and beats a user-scope one. `claude mcp get vestibule` must show user
    scope and the bare name. `uv tool list` records the version.
  - **Windows and WSL:** Claude Code with the bare name, and the sandbox-only line. WSL makes
    "tested, Linux" true when the README is frozen.
  - **Windows:** Cursor, Claude Desktop.
  - **The pass check proves the property:** each runs `print(1)` and shows the expected isolation. The
    sandbox-only session is also asked to list its tools and to run `whoami` with any shell tool. It
    must have only the two `mcp__vestibule__` tools.
  - **After:** the configs are removed and the tool uninstalled, so step 5 starts clean.
- **D. A dry pass on the two Linux VMs (V1):** restore `fresh-install`, start the VM, run
  `authorize.ps1`, in an admin window. Claude then runs the quick start's lines over SSH with the
  wheel, up to `setup`'s `ready` line, and reports any line that doesn't work as written. Restore
  again afterwards.
- **E. The recording (4b),** about 15 minutes.
- **F. The commits.** Step 4's merge to `main` waits until the release is due (§8).

## 8. What can go wrong

| Case | Handling |
|---|---|
| The README on `main` names `uv tool install vestibule-mcp` before that exists | Unavoidable, because RELEASE step 3 wants the release commit on `main` before the upload. Kept short: step 4 merges only when the release is due. |
| Someone registers the name in that window | The upload is the only real defense. Low odds, and the window is short. Stated, not solved. |
| A live session fails | Fix it if the config or `PATH` is at fault. Claude Desktop may fall back to "untested"; Cursor and Claude Code can't (§2.5). |
| The README's links into `main` 404 on the branch | Expected until the merge. Test 2 checks the paths against the tree meanwhile. |
| Fixing a word after RELEASE step 3 | The checklist says to go back to step 3. V1 moves what can be tried earlier so that's rarer. What stays for step 9 is Windows' first run and the index install. |
| `agg` draws Claude Code's glyphs as boxes | Choose another font with `--font-family`; failing that, V3's option 2. |
| The GIF is over 3 MB | Fewer columns, more speed-up, then `gifsicle --lossy`. |
| The model's text is withheld by a classifier, as in M3 5b | The tool calls still happen. A second take, or the fallback. |
| The README and `SECURITY.md` disagree | One home per fact (§2.7), and `critic` compares the two. |

## 9. Decisions

### V1 (Victor, 2026-11-09: (1)) — When the live sessions and a dry run happen
- **(1) Recommended: in this step,** before the release commit (§7, C and D). The README's marks are
  true when it's frozen, and a line that doesn't work is found before it can restart the release
  checklist. Step 5 still runs one GUI client on the release candidate, for criterion 3, and the full
  first runs.
- **(2) In step 5, as P6 first placed them.** The README then says "untested" for the GUI clients and
  for Linux's bare name, or claims them ahead of the test. Any word changed then restarts the
  checklist at step 3.

### V2 (Victor, 2026-11-09: (1)) — The headline (§2.2)
- **(1) Recommended: change it as §2.2 drafts.** It's your wording from July, so it's your call, but
  "nothing sent to a cloud service" isn't true for an agent on a hosted model.
- **(2) Keep it,** with the correction further down the page.

### V3 (Victor, 2026-11-09: (1)) — How the GIF is made
- **(1) Recommended: `asciinema` and `agg`, in WSL.** It records what the terminal really received,
  stays small, and Claude can do the conversion. Claude sets both up in the kit.
- **(2) ScreenToGif on Windows.** Exactly what you see, fonts included, but you do the trimming by
  hand, and its size is harder to predict.

### V4 (Victor, 2026-11-09: (1)) — `read_workspace`'s race

> **Done 2026-11-09** in its own step: [`M4-step4-workspace-race.md`](M4-step4-workspace-race.md).
> Victor took its X3 (1), which changes the Windows half of option (1) below: Windows doesn't
> check the opened handle's path, it does the same walk as POSIX (each name opened from the
> folder already open, a link never followed). §3's "`read_workspace`'s jail, and its race" and
> its added residual take that plan's §8 as their text: what holds on a local drive, what a
> Windows server with its workspace on a network or WSL share leaves, what a directory listing
> leaves, and that a file's content can still change while it's read.

The walk checks each path component, then opens the whole path, and `O_NOFOLLOW` guards only the last
one. A guest running at the same time can swap a checked folder for a symlink in between, and on
Windows it can swap the last component too. `verifier` reproduced it against the real `workspace.py`,
with the swap made from the host: the call returned a file outside the workspace, about 1.5% of the
time in a 4 s race on WSL, and less often on Windows. A real guest wasn't run. M1 accepted this as a
residual "documented in the tool description and SECURITY.md later". The description mentions
concurrent changes (`server.py:133`) but not that a host file can come back.

It matters more than M1 weighed it: the result goes to the model, so a prompt-injected run could
bring `~/.ssh/id_ed25519` into the agent's context, and the guest can learn the username from the
mount table.

- **(1) Recommended: fix it before the release, as a small step of its own** with its own plan and the
  full loop. On POSIX, walk with `dir_fd` and `O_NOFOLLOW` so nothing is resolved twice. On Windows,
  check that the opened handle's final path is inside the workspace. `SECURITY.md` then says what holds.
- **(2) Document it only,** as residual 12, with one more sentence in the tool description. Faster, and
  the first release ships a known way to read a host file.

## 10. Build order, each with its check

**4a, the text:**
1. Tests 1–3 and 6 of §5. Check: red on today's README, for the stated reason.
2. `SECURITY.md`, with test 4 and §5's item 5. Check: test 4 green; every claim traced to its source by
   re-reading it.
3. The README. Check: tests 1–3 and `test_pins.py` green; `readme_renderer` accepts it.
4. The other docs of §4. Check: `test_docs.py`.
5. The mutants of §5, except the GIF's.
6. §7's A, B, C and D, and the marks and the macOS sentence they settle.
7. The gate on both platforms.
8. `critic` on the README and `SECURITY.md` (criterion 6 asks it of `SECURITY.md`; the README is a
   published claim too), then `verifier` on every finding.

**4b, the demo:** the kit; a dry run of the two snippets in a real sandbox; Victor's take; the
conversion, the transcript comparison and the frame check; the README's line and test 7, with its
mutant; `test_docs.py` and `test_pins.py` on both platforms; `critic` on the caption.

**Size:** `SECURITY.md` about 190 lines, the README about 100 lines longer, `test_docs.py` about 70,
`test_pins.py` about 4.

## 11. Files

| File | Change |
|---|---|
| `README.md` | §2, and the GIF in 4b |
| `SECURITY.md` | new, §3 |
| `tests/test_docs.py`, `tests/test_pins.py` | §5 |
| `docs/demo.gif` | new, 4b |
| `docs/GETTING_STARTED.md`, `docs/ARCHITECTURE.md`, `docs/RELEASE.md` | §4 |
| `docs/plans/M4/M4-adoption.md` | dated amendments |
| `docs/HISTORY.md`, `docs/BUILD_LOG.md`, this plan | as usual |

**Not touched in this step:** anything under `src/`, `scripts/`, `pyproject.toml`, the sdist's include
list, `CLAUDE.md`.

## 12. Review (`skeptic`, then `verifier`, 2026-11-09)

`skeptic`'s verdict on v1 was "sound with amendments". `verifier` confirmed all 19 checkable claims.
Its corrections are noted.

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | high | `SECURITY.md` would advertise the `read_workspace` jail and drop the race M1 assigned to it. Verifier: reproduced with a host-side swap on WSL and Windows; the description does mention concurrency (`server.py:133`); nine plans carry items, not five | V4; residual added; §1's count |
| 2 | high | "How it works" as written keeps "structurally impossible" on the frozen PyPI page. Verifier: also `:185`, `CLAUDE.md:96` | §2.2 rows; ARCHITECTURE reworded; `CLAUDE.md` left to Victor (§4) |
| 3 | med | "Tested, Linux" written before Linux is tested | §7, C adds a WSL session |
| 4 | med | The bare-name session can pass on the dev venv's local-scope entry; step 0's v0.0.1 is still installed. Verifier: local beat user in a throwaway config | §7, C's before-steps: an empty folder, `claude mcp get`, `uv tool list` |
| 5 | med | The pass check can't detect a sandbox-only line that failed open; the kit's flags differ; `--restricted` unweighed | The check now proves the tool list; `dontAsk` and `--strict-mcp-config` named (§2.3). **`--restricted` weighed and not taken:** it keeps the file tools and ignores settings files; F4 and both kits use `--tools ""` |
| 6 | med | No new terminal after uv's installer. Verifier: `Add-Path` never updates the current session | §2.4's first row |
| 7 | med | `curl` is missing on stock Ubuntu; `sudo` may be missing on Debian | §2.4 rows |
| 8 | med | The Windows line isn't the measured sequence, and nothing would run it. Verifier: the order comes from the contract | Worded as the fix for the failure measured; a contract amendment |
| 9 | med | Cursor can fall back to "untested", but criterion 5 requires it | §2.5, §8 |
| 10 | med | V1's reasoning applies to more than the GUI sessions | §7, D's dry pass on the Linux VMs; V1 |
| 11 | med | The replacement sentence overclaims for degraded runs. Verifier: `auto` reaches `native-degraded` too | §3's departures scoped to `native` and `container`, with the exceptions named |
| 12 | med | "All three", "never touching the real system" and "untouched" contradict the fixes | §2.2 rows |
| 13 | med | The prompt's second call has nothing to send | Both calls are fixed code (§6) |
| 14 | med | The on-screen `isolation:` line is typed by the model | Compared with the transcript; the caption says so |
| 15 | med | `agg`'s default idle limit cuts pauses non-uniformly | An explicit `--idle-time-limit`; the caption says "pauses shortened" |
| 16 | low | Other "as written" lines go stale (`:84`, `:87`, `:89`) | §2.2 rows |
| 17 | low | Two more carry-forwards: a remote daemon's clock, the Apache-2.0 attribution | §3 |
| 18 | low | Uninstall: digests can go stale; `vestibule-*` in the temp folder; `sudo` | §2.8; `test_pins.py`; RELEASE step 2 |
| 19 | low | The tests miss `<img>`, reference links and `tree/`; file-level citations; the heading check is one-off | §5, items 1–5 |
| 20 | low | 4b has no gate or `critic`; a mutant scheduled before its file; "red" can't hold for tests that need `SECURITY.md` | §10 reordered |
| 21 | low | Criterion changes missing from the amendment list; P6's clock on Debian | §4's list |
| 22 | low | Step 3 is already on `main` | §7, A |
| 23 | low | Debian's conditions; "can't" should be "isn't designed or tested"; Podman's caveat | §2.1, §2.4 |
| 24 | low | The README grows nearer 100 lines | §10 |

## 13. As built: 4a (2026-11-09)

**Built as §2–§5 say, with what the build and the reviews changed:**

- **The sandbox-only session (§2.3) says what the live sessions showed.**
  - The plain line left 69 tools in Windows Claude Code: Vestibule's two, plus the other MCP
    servers and the account's connectors. §7 C's check, "it must have only the two
    `mcp__vestibule__` tools", held only with the stricter line.
  - With `--strict-mcp-config --mcp-config <file> --permission-mode dontAsk` the agent had
    Vestibule's two tools and a few of Claude Code's own (`EndConversation`, `advisor`). The
    README now shows that line in full, for bash and PowerShell.
  - Neither line stops a plugin's hooks. The stricter one drops a plugin's MCP server, but a
    plugin's Stop hook ran a script on the host after every turn in both sessions. The README
    says so.
- **The quick start (§2.4) puts uv's install inside each platform's steps,** after that
  platform's prerequisites: `curl` is one of them, and the first draft used it before installing
  it. `setup` "prints" its ready line: it doesn't end with it.
- **The client table (§2.5): all three tested.** Claude Code on Windows and in WSL with the short
  name, Cursor and Claude Desktop on Windows. Claude Desktop 4.23118.1 has no "Edit Config"
  button, so the row gives the file's location.
- **Troubleshooting (§2.6) quotes the messages as the code prints them.** No message says
  "AppArmor", and none mentions the `docker` group: a user who isn't in it is told to start
  Docker. The README gives the real fix. The message itself is a known issue.
- **`SECURITY.md` (§3) grew to 315 lines** against about 190, and has 14 residuals:
  - **§2 says how each verdict is reached,** which §3's table didn't ask for. A container run's
    verdict comes from the selection check, and that check reads back only the resource caps,
    `/tmp`'s size and the workspace's mode, and on cgroup v1 not even the caps.
  - **Residual 14 is new:** the container backend trusts the runtime for the flags it can't read
    back, and doesn't set Docker's seccomp profile.
  - **§3.9 says exactly when `auto` ends on `native-degraded`,** and keeps the old README's line
    about Linux before 5.14.
  - **`none` is spelled out:** it means no isolation was confirmed, not that nothing ran.
  - **Residual 12 is the race fix's leftovers** (a workspace on a share, and listings), from
    [`M4-step4-workspace-race.md`](M4-step4-workspace-race.md) §8.
  - **The header says what the tests check:** that each test and file named exists, not what a
    section or row says.
- **macOS (§7 A):** run #1 of the manual job passed on `b8565cf`, with 849 passed, 29 skipped and
  159 deselected. The skips are Linux-only tests and one `read_workspace` test whose file name
  macOS's filesystem refuses. Both documents say the Docker-free tests pass there and that no
  sandboxed run has been made on a Mac. This is criterion 10's record.
- **Private vulnerability reporting (§7 B) is on.**
- **The dry run (§7 D) passed on both VMs,** restored to `fresh-install` first. Every
  quick-start line worked as written, with the wheel in place of the PyPI name, up to `setup`'s ready line, which is where §7 D stops. `claude mcp add` and the `print(1)` run weren't made on the VMs, which have no Claude Code; the live sessions made them on Windows and in WSL.
  Debian's `setup` printed `isolation native` and Ubuntu's `isolation container`, with its
  reason for not using native. On Debian, `sudo reboot` prints a bus warning and still reboots.
- **The package count (§3) was re-measured** with `uv pip compile` for both platforms: 30 and 31.
- **"The demo used both"** (§2.3) waits for 4b (§14: it did).

**Tests, where they differ from §5.** `builder` wrote them over five rounds:
- **Tests 2 and 3 couldn't be red on the old README:** it had no absolute link for them to
  check. Each now asserts that it looked at something.
- **Test 2 checks more than §5 asked,** all from review findings:
  - a GitHub URL must be on `https://github.com` or `raw.githubusercontent.com`, and in this
    repo;
  - only the repo page, `blob/main`, `tree/main` and raw `main` forms pass;
  - a raw URL names a file, and an image into this repo uses a raw URL;
  - a path git ignores fails. A file that is untracked and not ignored passes, because Claude
    doesn't stage files and the gate runs before the commit.
- **Test 3 also checks anchors into other files of the repo,** and forbids a query on the repo
  page.
- **Test 4 flags a test citation written without `tests/`.**
- **Mutants:** 39 red on their named tests, plus the 7 controls that must stay green: all on
  Windows 3.11, and each round's new ones on WSL 3.14.
- **Size:** `tests/test_docs.py` grew by 187 lines against about 70.

**Review of the two documents:** `critic` three times, `verifier` on each round's checkable
findings.
- **Round 1:** 20 findings (6 medium). `verifier` confirmed 12 of 15 and refuted 3.
- **Round 2:** 14 findings (1 high). `verifier` confirmed all 10 it was given. The high one was
  a sentence a round-1 fix had added: it said a container's protections were read back from
  inside.
- **Round 3:** 11 findings (1 high). `verifier` confirmed all 8 it was given. The high one was
  another added sentence: "none of them ran a shell command", when a plugin's hook had.
- Every confirmed finding is fixed.
- **Round 4,** narrow, on the sentences round 3 changed and on these records: 1 medium and 4 low,
  no high. The medium one was in the records, which said every quick-start line ran on the VMs.
  The main session checked each against the logs and fixed all five; `verifier` wasn't run on
  them.

**Not changed, and recorded as known issues:** `setup`'s message for a user outside the `docker`
group; the README and `SECURITY.md` state some facts twice (§2.1 keeps the README's reference
sections, and §3 has `SECURITY.md` cover the same layers), so one can drift from the other.

**Size:** the README is 361 lines, 145 more than before, against "about 100 longer".

**Gate:** green on the final tree.
- **Linux (466 s of 540):** 3.14 full suite 1042 passed, 0 skipped; 3.11 Docker-free 992;
  stdio 7.
- **Windows:** 850 passed, plus 172 POSIX/Linux-only skips, none of them for Docker.
- The README renders with PyPI's Markdown renderer: 15 code blocks, 6 tables, no relative link.

## 14. As built: 4b (2026-11-10)

**The GIF** is `docs/demo.gif`: 719,281 bytes, 19.7 s, 983×560, 91 frames. The README shows it
under the badges, with its caption.

**How the take was made, where it differs from §6.** Six takes; the sixth is the GIF.
- **The kit** is `~/vestibule-live/demo/` in WSL: `plant.sh`, `prompt.txt`, `session.sh` and
  `take.sh` (§6's `record.sh`). `session.sh` shows the key file on the host, then starts Claude
  Code with `start.sh`'s flags and `--model claude-opus-5-5`.
  - The flags are the README's stricter line's. The config file is the kit's `mcp.json`, not
    the README's JSON: it starts the development venv's `vestibule-mcp` through `sh -c`, with
    the server's stderr appended to a log.
- **The prompt is pasted once the session is up.** Takes 1 to 3 passed it as a start-up
  argument. In all three it was sent 0.4 to 0.7 s before the server answered its tool list
  (and submitted 0.86, 0.58 and 0.72 s before), so the agent was offered no tools at all, and
  no call was made.
- **The model is Opus 5.5.** Takes 1 and 2 ran on that machine's default, Fable 5.2. Its
  safeguard stopped the reply both times and Claude Code switched to Opus 4.8 (§8's classifier
  case). Take 3 named Opus 5.5 and reworded the prompt's first lines.
  - **Not tried:** Fable 5.2 in a session that had `run_code`. Both of its takes had no tools
    and the first wording of the prompt.
  - **What Opus 4.8 did with no tools:** it typed a Bash call as plain text in both takes. In
    take 1 it also typed an output it made up, a key that isn't what the planted file holds.
    This is the case the transcript comparison below is for. Opus 5.5, in take 3, said it had
    no `run_code` and wouldn't substitute a tool or invent a result.
- **Two messages, one call each.** In takes 4 and 5, with two different prompts, the model
  made both calls in one turn with no text between them, so call 1's result never showed. The
  first message now holds both calls' code and asks for call 1 only. The second is
  "now call 2".
- **Call 2 opens a raw socket** to `203.0.113.10`, port 80, with a 3 s timeout, and would then
  send the placeholder string. §6 had `urllib`, whose uncaught error is a 35-line traceback.
  The address and the error (`Network is unreachable`) are the same.
  - **On the host the same line behaves differently:** run outside the sandbox in WSL, it
    times out after 3.03 s. In the sandbox it fails at once (the whole run took 124 ms). The
    GIF doesn't show that comparison.
- **Recorded at 100×24** with asciinema 2.4.0 and rendered by agg 1.9.0, whose binary was
  checked against the release's sha256.

**The session:** Claude Code 2.1.322 with `claude-opus-5-5`, and Vestibule from the WSL
development venv, an editable install of this working tree (`5a1bc23`; no `src/` change since).
- **The tools the agent was offered:** Vestibule's two, and Claude Code's own `EndConversation`,
  `advisor` and `DeferredToolPlaceholder`. So §2.3's "The demo used both" holds: it used the
  stricter line's flags.
- **The audit log has both calls** as `ran`, `isolation=native`, exit 1.

**The edits are all in time. No byte of the terminal's output before the end cut is changed.**
They go past §6's list (the banner cut, shorter pauses, one speed-up) in three ways, marked
below, and the caption names each.
- **One stretch takes no time:** from the end of the key listing (3.4 s on the take's clock)
  to the moment the last line of Claude Code's banner left the screen (20.94 s). Its events
  are moved to one instant and none is dropped, so the terminal's state stays exact. It holds:
  - the banner and one notice;
  - **past §6:** the pasting of the prompt, and the first 2.4 s of the agent's turn. The first
    frame after the key listing shows call 1 already under way.
- **Past §6: the end is dropped** after 48.9 s, just after the second result. The take runs to
  550 s. The dropped part holds scrolling back to the banner, a "copied to clipboard" line,
  Claude Code's recap and `/exit`.
- **Pauses are capped at 2 s** (`agg --idle-time-limit 2`), which removes 10.5 s: most of it is
  the wait while "now call 2" was typed.
- **One speed-up, 1.3×** (`--speed 1.3`).
- **Past §6: the last frame is held 4 s** (`--last-frame-duration 4`).
- **Not an edit, and in the GIF:** the view was scrolled during the take. In frames 29 to 37
  it jumps up to the prompt and back, and in frame 89 it is scrolled up a few lines, so call
  2's `isolation:` line is out of view for 0.4 s. The last frame shows it.

**Checked before it was kept:**
- **Against the transcript:** the text the agent typed after each call is the tool's own
  result inside a code block, character for character, for both calls.
- **Every screen state:** the cut recording was replayed in a terminal emulator. Its 128 states
  after the key listing hold 120 distinct lines: none from the banner, and no email, plan or
  account detail. The only name on screen is the Linux user name in the paths, which is the
  repo owner's GitHub name.
- **Every frame:** Claude looked at all 91 on contact sheets at one-third size, which shows a
  banner or a notice but can't be relied on for reading text, and at five frames full size.
  The replay above is the check of the text. Victor looks at the GIF before the commit.
- **On screen, not Vestibule's:** Claude Code's spinner words, "Cooked for 5s · done 4:12 PM"
  after call 1 and "Brewed for 5s · done 4:12 PM" on the last frame, a `/voice` tip, "don't
  ask on (shift+tab to cycle)", "paste again to expand" and "Jump to bottom".

**Test 7,** by `builder`: `test_demo_gif_is_under_3_mb` reads the file, checks its first six
bytes are a GIF signature, and checks it is under 3,000,000 bytes (the stricter reading of
criterion 7's "3 MB").
- **It couldn't be red first:** the GIF existed before the test. The mutant that deletes the
  file stands in for that.
- **Mutants, 3 red of 3,** each after a green control, on Windows 3.11 and WSL 3.14: the file
  padded to exactly 3,000,000 bytes, the file with a PNG signature, and the file missing. One
  byte under the limit stays green. With the file missing, 4a's test 2 fails too, on the
  README's image line.
- **`test_docs.py` and `test_pins.py`:** 16 passed on Windows 3.11 and on WSL 3.14 and 3.11.
  ruff and mypy are clean. The full gate wasn't re-run: this step changes no `src/`, and adds
  one test that needs no sandbox.

**Review:** `critic` on the caption and these records found nothing high, 3 medium and 8 low.
`verifier` confirmed all 9 it was given, three with corrections. All are fixed.
- **The caption's list of edits was incomplete** (the 2.4 s, the end, the held frame), and it
  said Windows and stock Ubuntu get `container` without "with Docker".
- **This section's account of the failed takes was wrong:** it had one take with no tools, not
  three, and gave the safeguard as the reason for Opus 5.5 without saying what wasn't tried.
- **The kept copy of the take was cut short,** copied while the session was still open. It is
  the whole take now, and the cut recording rebuilds from it byte for byte.
- **A second, narrow pass** by `critic` on the changed sentences found 5 low and nothing
  higher: the caption's "with Docker" was too broad for Ubuntu (the snap can't run Vestibule),
  the timings measured submission where they said sending, one frame was described wrongly,
  and a rule was drawn from three takes. The main session fixed those five and re-ran the doc
  tests and the README render; `verifier` wasn't run on them.

**Kept outside the repo,** in `m4spike\demo\`: the take, the cut recording, the kit's scripts,
the checking scripts and the exact commands (`HOW.txt`).

**Left for step 5:** criterion 7's "renders on the PyPI page". The image's URL is on `main`, so
it shows nothing until this branch is merged.
