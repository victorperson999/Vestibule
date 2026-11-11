# M4 contract — install, first run, and the threat model

Status: **signed off 2026-11-07 (v2).** v1 was reviewed by `skeptic` (19 findings: 3 high,
12 medium, 4 low; verdict "sound with amendments"). `verifier` checked the 15 checkable
claims: 8 confirmed, 4 confirmed with corrections, 2 plausible, 1 mostly refuted. It also found
two more problems: the sdist leak (now high) and snap Docker (§8). Every finding is taken into
v2. Victor chose the recommended option on each of P1–P9 (§2), with P2 amending M2's S1-D1.
Everything else is fixed, with one defensible answer each.

Inputs:
- `docs/PLAN.md` §Milestone 4: one-command install and copy-paste MCP configs for Claude Code
  and Cursor; a 20-second demo GIF; a "ruthless" first-run test on a fresh machine; an honest
  `SECURITY.md`. Its acceptance: *a stranger can install and run it in under 2 minutes on a
  clean machine following only the README.*
- Work earlier milestones assigned to M4, each with its source:
  - `SECURITY.md`'s contents: M1 step 7 (`M1-step7-acceptance.md:242`), M1 S6's pinning
    tradeoff (`M1-step6-pinning.md:157`), M2 §7's carry list (`M2-step7-acceptance.md:280–288`),
    M2 step 3 (`M2-step3-warden-core.md:639`), M3 §8 (`M3-step5-acceptance.md:411`);
  - the stock-Ubuntu VM run: M2 contract N5 (`M2-native-warden.md:368`), S6-D4
    (`M2-step6-selection.md:475`);
  - the release checklist's wheel smoke test: `M2-step7-acceptance.md:285–288` (V13);
  - leftover container run dirs after a hard kill: M3 contract §1;
  - the `mcp<2` move: signed in M2's S1-D1 as "before the PyPI release" (HISTORY), listed as
    "Later" in M3 step 5 §8, and "before M4's PyPI release" in BUILD_LOG;
  - candidates, not commitments: a doctor command (M1 S6-D3), a custom image (M1 contract),
    the Windows→WSL bridge (M2 E2).
- The code as of `c615671` (M3 accepted). The GitHub repo is public.
- Facts checked on 2026-11-07 (§7).

---

## 1. Scope

**In:**
- **A public release, 0.1.0 on PyPI,** installed with one command, so nobody clones the repo
  (P1). Victor uploads; Claude prepares the build and the checklist (F1, F2).
- **One setup command on every platform** (P4), with a readiness check at its end (P5).
- **A README a stranger can follow** (F4):
  - a quick start per platform, and copy-paste configs for Claude Code, Cursor and Claude
    Desktop;
  - **what the boundary is:** only code sent to `run_code` is sandboxed, and how to make it
    an agent's only way to run code;
  - an uninstall section, and the demo GIF (P7).
- **`SECURITY.md`** (F5): the threat model, every residual earlier milestones carried here,
  and how to report a vulnerability (P8).
- **The first-run test** on fresh machines, against the release candidate before the upload,
  timed against the 2-minute budget (P6). It also settles M2's open "expected" rows where a VM
  can: the stock-Ubuntu AppArmor path, and Debian.
- **Leftover container run dirs after a hard kill** (F3), the one lifecycle fix assigned here.

**Out, with reasons:**
- **The mcp 2.x move** (P2). It rewrites the protocol layer (handler signatures,
  error shaping, cancellation, input validation) and touches M2 step 1b's shutdown rules, so
  it needs its own contract. The SDK's docs endorse `<2` for projects not ready to move.
- **The Windows→WSL bridge** (M2 E2). The workspace, paths and shutdown signals would cross
  the WSL boundary, and none of that is designed or measured. A later candidate.
- **A custom published image** (M1). Owning image CI and its supply chain is a project of its
  own.
- **The Windows shutdown race's liveness lease** (BUILD_LOG). A later milestone, as recorded.
- **Container runs on macOS.** GitHub's macOS runners have no Docker, and there is no Mac here.
  P9 covers what can be run there.
- **arm64 runs.** No arm64 host; the README keeps "untested".
- **Standing CI.** PLAN.md doesn't ask for it. P9's one manual job is not a standing pipeline.
- **Retention, a tamper-proof audit, network policies, Podman validation:** declined or
  deferred by earlier contracts; nothing here changes that.

---

## 2. Decisions

### P1 (Victor, 2026-11-07: (1)) — How a user installs it

Every message that names the setup fix tells the user to run `vestibule-mcp setup`
(`rootfs.py:84–99`, `select.py:486–490`, `native.py:106–107`), and the README's audit line is
`vestibule-mcp audit`. Whatever the install, those commands must work as printed.

- **(1) Recommended: `uv tool install vestibule-mcp`.**
  - It puts `vestibule-mcp` on `PATH`. A winget install of uv may first need
    `uv tool update-shell` and a new terminal, which the quick start says. Every existing
    message then stays true.
  - The version stays fixed until `uv tool upgrade vestibule-mcp`, so `setup` and the server
    are always the same version.
  - uv downloads a suitable Python itself when there's none, so a clean Windows machine needs
    no Python install.
  - The launch chain is the same as today's venv install: the console script, then Python.
    Nothing new sits between the client and the server, so S1-D5's signal handling is
    unchanged.
  - GUI clients, which may not inherit the shell's `PATH`, get the absolute path from
    `uv tool dir --bin`. The README shows that.

> **Amendment 2026-11-08 (step 0, [`M4-step0-spike.md`](M4-step0-spike.md) A-1 and B-6, approved by Victor):**
> - On Windows, Claude Desktop and Cursor found the bare `vestibule-mcp` after
>   `uv tool update-shell`, with no re-login, so the Windows README uses the bare name. The
>   client must start after `update-shell`.
> - The `uv tool dir --bin` advice stays for macOS, which step 0 didn't measure.
> - A-1's question, whether uv's own installer puts `.local\bin` on `PATH`, wasn't measured.
>   Step 5's first run installs uv as the README says, and answers it.

- **(2) `uvx vestibule-mcp`,** the form many MCP servers document.
  - Nothing goes on `PATH`, so every message naming a command must become
    `uvx vestibule-mcp …`.
  - The version is resolved at the first run and then cached until a cache clean, so `setup`
    and a GUI client's server can resolve different versions.
  - On Linux, uv forwards SIGINT and SIGTERM to the server (measured, §7). On Windows the
    chain grows by two processes (`uvx.exe` → `uv.exe` → …).
- **(3) `pipx install vestibule-mcp`,** as PLAN.md names it. It needs Python ≥ 3.11 and pipx
  first, and a clean Windows machine has neither.

Every option keeps the clone path in GETTING_STARTED for development.

### P2 (Victor, 2026-11-07: (1); amends M2's S1-D1) — Which MCP SDK the release depends on

The facts (§7):
- **The lines:** mcp 2.3.0 is the stable line, and `pip install mcp` gets it. 1.x is in
  maintenance ("critical fixes and security patches"); its newest release is 1.30.0
  (2026-09-07).
- **What 2.x brings:** it serves the 2026-07-28 protocol revision as well as the 2025 one, so
  it answers Claude Code's `server/discover` probe, today a 31-error warning (BUILD_LOG).
- **What 2.x changes:** it no longer validates a call's arguments against the input schema
  before the handler runs. It turns an exception into a sanitized `-32603`, and every handler's
  signature changes.
- **Today's install:** a fresh one already gets mcp 1.30.0 and anyio 4.16.0, versions no gate
  has run, while the constraints pin 1.28.1. `mcp/server/stdio.py` and `lowlevel/server.py`
  are byte-identical between 1.28.1 and 1.30.0, but nothing checks that.

- **(1) Recommended: release on 1.x, with the published range narrowed to the minor the gates
  ran (`mcp>=1.30.0,<1.31`); the 2.x move gets its own milestone after M4.**
  - Before the release, F1 re-freezes the constraints from a fresh resolve, and both gates run
    on that set. Every live session so far works on the 2025 protocol, and the SDK's own docs
    recommend a `<2` pin for projects not ready to move.
  - This **amends M2's S1-D1** ("moving to mcp 2.x is a planned task before the PyPI release",
    HISTORY). It reconciles S1-D1 with M3 step 5 §8 ("Later"). S1-D1's reason was that an
    unpinned install couldn't start, and the pin has fixed that since M2 step 1.
  - The cost of the narrow range: a 1.30.x patch reaches users, a 1.31 doesn't until a
    Vestibule release.
- **(2) Move to 2.x first, as M4 step 1, then release.** The first public release then
  depends on the line that gets features, and the `server/discover` warning goes away.
  - The cost: a protocol-layer rewrite with its own spike, plan and review, ahead of
    everything else in M4.
  - Several published claims change with it, such as the README's "a call that fails the input
    schema leaves no record".

### P3 (Victor, 2026-11-07: (1)) — Stock Ubuntu 23.10 and later

Ubuntu restricts unprivileged user namespaces by default
(`kernel.apparmor_restrict_unprivileged_userns=1`), so native isolation can't start there.
M2's N5 shipped no AppArmor profile, because a profile attaches to the interpreter's real path
and would grant user namespaces to every `python3` on the system. On a stock Ubuntu 24.04 LTS,
`auto` therefore picks the container backend if Docker works, and otherwise refuses with the
AppArmor message.

The container path has costs of its own, and the README must state them:
- **Only Docker Engine from Docker's apt repository works.** Ubuntu's own suggestion,
  `sudo snap install docker`, can't run Vestibule:
  - the snap can't see dot-directories in `$HOME` (`~/.vestibule/workspace`) or the host's
    `/tmp`, where run dirs live;
  - its README says `--security-opt no-new-privileges=true` stops containers from starting,
    and Vestibule always passes it (§7).

> **Amendment 2026-11-08 (step 0, [`M4-step0-spike.md`](M4-step0-spike.md) B-5, approved by Victor):** measured on snap Docker 29.8.0:
> - `no-new-privileges` alone stops containers from starting, so that is the blocker.
> - The dot-directory reason is wrong on this snap: a bind mount of `~/.vestibule/workspace`
>   worked. A bind mount of a host `/tmp` folder came up empty, and run dirs live there.
> - The conclusion stands: only Docker's apt repo works. Victor chose that Vestibule's error
>   names the snap and points to that repo (§6, step 2).

- **The `docker` group is root-equivalent** (Docker's own docs). Rootless Docker avoids that,
  and on 24.04 its `docker-ce-rootless-extras` package ships the AppArmor profile it needs.

- **(1) Recommended: keep N5.** The Ubuntu quick start installs Docker Engine from Docker's
  apt repo, names the group's cost and the rootless option, and says why native isn't used.
  `setup` skips the native trees there (P4). The first-run test confirms both paths on a real
  VM, installing Docker exactly as the README says (§5, criterion 4).
- **(2) Spike a narrower profile first.** A uv-managed Python has a real path of its own, so a
  profile could name only that interpreter. That is still every script it runs, it takes one
  `sudo` step, and it changes a signed security decision. If the spike holds, it becomes its
  own reviewed decision; M4 still ships (1).

### P4 (Victor, 2026-11-07: (1)) — One setup command everywhere

Today the README has three setup paths:
- `vestibule-mcp setup` on Linux, which downloads both native trees on any Linux, even where
  user namespaces are refused (`rootfs.py:102–157`, §7);
- two `docker pull …@sha256:…` lines on Windows and macOS;
- both on a Linux host that wants the container fallback.

- **(1) Recommended: `vestibule-mcp setup` does what this machine needs, in this order.**
  1. **Linux only:** check that unprivileged user namespaces work, then unpack the native
     trees.
     - If they don't work (stock Ubuntu's AppArmor), it skips the 123 MB and says why.
     - The selection message for that case stops sending the user back to `setup`.
  2. **The readiness check (P5).**
  3. **The images:** if the result isn't full `native` and a container runtime is installed,
     it pulls the two pinned images with the runtime's own `pull` of the exact refs, then
     checks again.
     - On Windows and macOS that is the normal path.
     - On a Debian host with Docker, native is full, so the 123 MB of images are skipped.

  Every step prints to stderr, and nothing is ever pulled during a tool call (M1's D2).
  `setup` exits 0 when the final check says ready, whatever an optional step skipped.
- **(2) Keep the `docker pull` lines.** No code; the README stays per platform.

> **Amendment 2026-11-08 (step 2, [`M4-step2-setup.md`](M4-step2-setup.md) §4, approved by Victor):**
> - `setup` pulls only the images that are missing, so a second run needs no network.
> - An image the machine needs that wasn't confirmed or pulled exits 1, even when the check
>   says `container`. The probe uses only the python image, so otherwise every node run
>   would be `Blocked:` after a `ready`. On a native or native-degraded result it changes
>   nothing.
> - `VESTIBULE_BACKEND` narrows what a machine needs: `container` skips the trees, `native`
>   skips the images, `naive` skips both. A root user skips the trees too.

### P5 (Victor, 2026-11-07: (1)) — Does setup end with a readiness check?

- **(1) Recommended: yes, run the server's real selection once and print what it returns.**
  - The output is `ready in this terminal's environment: isolation native`, or the exact
    `Blocked:` message with its fix.
  - It also prints the inputs that can differ for a client started elsewhere: the container
    runtime's resolved path, and any `VESTIBULE_*` overrides set in this shell.
  - The README tells GUI-client users to put the runtime's directory in the config's
    `env.PATH` when their client doesn't inherit it (macOS's Claude Desktop, for one).
  - This is M1 S6-D3's "doctor", folded into the existing command. It prints selection's own
    result, so there's no second wording to keep honest.
  - The run must close the selector the way the server does (`aclose()`, then the hard-exit
    rule, `server.py:510–535`). Otherwise the probe's finisher tasks hit the Windows
    loop-close hang found in M1 step 5.
- **(2) No.** The first tool call stays the doctor (S6-D3).

### P6 (Victor, 2026-11-07: (1)) — The first-run test: who, where, and what the clock counts

**The clock** (either option):
- **Start:** the README is open, and the platform's other prerequisites are installed: Claude
  Code, logged in, and Docker where the quick start needs it.
  - Docker Desktop alone takes longer than 2 minutes, and it's its vendor's install.
  - **uv's install is inside the clock,** since this project adds it, along with any new
    terminal it needs.
- **Stop:** the agent's first `run_code` result, with the isolation the README promises for
  that platform.
- **Everything between counts,** downloads included, on a VM network adapter capped at
  25 Mbit/s (`Set-VMNetworkAdapter -MaximumBandwidth`). A fast home line would hide a slow
  stranger's minute.

**Against what:** the release candidate wheel, before the upload, with one recorded
substitution: `uv tool install <wheel>` in place of `uv tool install vestibule-mcp`. Then a
short confirmation from PyPI after the upload (§6, step 5).

**The machines:** fresh Hyper-V VMs on Victor's PC (Windows 11 Pro has Hyper-V).
- **Stock Ubuntu 24.04 LTS:** once without Docker (expect the AppArmor `Blocked:`), and once
  with Docker Engine installed as the README says (expect `container`).
- **Debian 13, a minimal install with no desktop:** expect `native`. If the user bus is missing
  (`dbus-user-session` is only recommended), the README's prerequisites name the package, and
  the run follows them.
- **Windows 11:** Docker Desktop inside a VM needs nested virtualization. If that fails, a new,
  empty Windows user account on Victor's PC stands in. That is weaker, and it's recorded as
  such: the account's Docker image store must be checked empty first.
- **The GUI clients:** one live session each with Cursor and Claude Desktop on Windows (both
  free). PLAN.md names Cursor, and GUI clients are where `PATH` goes wrong.

- **(1) Recommended: Victor runs it, following only the README; Claude doesn't help.** Victor
  knows the project, so a step filled in from memory could hide a gap. The rule against that:
  any command typed that the README doesn't show fails the run. A real stranger's run, if one
  can be arranged, is recorded on top.
- **(2) A real stranger is required.** This is closest to PLAN.md's words, but it depends on
  finding one, and on watching them without helping.

> **Amendment 2026-11-09 (step 4, [`M4-step4-docs.md`](M4-step4-docs.md) V1 and §4, approved by Victor):**
> - The live sessions (Claude Code on Windows and in WSL with the short name, Cursor, Claude
>   Desktop) and a dry run of the quick start on the two Linux VMs happen in step 4, before the
>   README is frozen. Step 5 still runs the full first runs, and one GUI client on the release
>   candidate.
> - On a minimal Debian, the three packages and the reboot they need are prerequisites outside
>   the clock, like Docker.

### P7 (Victor, 2026-11-07: (1)) — The demo GIF

- **(1) Recommended: a real recorded session, trimmed to about 20 s.**
  - Linux Claude Code, started with only Vestibule's tools (the live-check kit's launch).
  - The prompt asks it to read a planted fake key at an absolute path and send it to a URL.
    Both fail on screen with their real results (`No such file or directory`, `Network is
    unreachable`) and their `isolation: native` line.
  - **Fallback,** if the model declines the direct request: the instruction sits in a
    workspace file the agent is asked to follow, which is the prompt-injection case itself.
  - **The caption says what it shows:** a session where `run_code` was the agent's only way to
    run code; native is Linux's; Windows, macOS and stock Ubuntu get `container`.
  - Victor records it; Claude writes the prompt and the canary script. It lives at
    `docs/demo.gif`, under 3 MB, linked by an absolute URL so PyPI shows it.
  - Not `rm -rf /`: the workspace is writable, so that demo would really delete it.
- **(2) A scripted terminal recording** (a VHS tape, say) replaying a session. It's
  reproducible, but it's a reenactment, and golden rule 5 makes a staged result a poor fit.

> **Amendment 2026-11-09 (step 4, [`M4-step4-docs.md`](M4-step4-docs.md) V3 and §6, approved by Victor):**
> - Both calls are fixed code in the prompt. The second posts to an address from the range
>   reserved for documentation (`203.0.113.10`), not to a hostname: a hostname would fail at DNS,
>   not with `Network is unreachable`.
> - It is recorded with `asciinema` and rendered with `agg`. The only edits are cutting the
>   opening banner, shortening pauses and one uniform speed-up, and the caption says so.
> - What the agent echoes on screen is compared with the tool's own results in the session's
>   transcript before the GIF is kept, and every frame is checked for anything private.

> **Amendment 2026-11-10 (step 4b as built, [`M4-step4-docs.md`](M4-step4-docs.md) §14; reported
> to Victor, who raised no objection and committed the step as `bc79e75`):**
> - The two calls are asked for in two messages. Both calls' code is in the first, and the
>   second is "now call 2". In one turn the model made both calls without showing the first
>   result.
> - The second call opens a raw socket to that address, not `urllib`, whose error is a 35-line
>   traceback.
> - The caption says macOS is *expected* to get `container`. No sandboxed run has been made on
>   a Mac (step 4a).
> - The edits go past the 2026-11-09 list. The stretch that takes no time also holds the
>   pasting of the prompt and the first 2.4 s of the agent's turn, everything after the second
>   result is dropped, and the last frame is held 4 s. All are edits in time, no output is
>   changed, and the caption lists them.

### P8 (Victor, 2026-11-07: (1)) — How vulnerabilities are reported

- **(1) Recommended: GitHub's private vulnerability reporting** on the public repo. Victor turns
  it on (Settings → Security → Private vulnerability reporting), and `SECURITY.md` links it. It
  needs no public email address.
- **(2) An email address** Victor chooses.

### P9 (Victor, 2026-11-07: (1)) — macOS

The server has never run on macOS; only monkeypatched `darwin` tests exist (§7).

- **(1) Recommended: one manually triggered GitHub Actions job on a macOS runner** (a
  `workflow_dispatch` workflow, not a standing pipeline). It runs:
  - the Docker-free tests;
  - the stdio end-to-end and signal tests;
  - F2's smoke test.

  It shows the server starts, answers and shuts down on macOS. Container runs stay untested
  there, and the README says so.
- **(2) No macOS run.** The README says "never run on macOS".

> **Amendment 2026-11-09 (step 3, [`M4-step3-release.md`](M4-step3-release.md) §4, approved by Victor):**
> `tests/test_stdio_e2e.py` is Docker-marked and these runners have no Docker, so on macOS "the
> stdio end-to-end tests" means the smoke test's real stdio session, plus `test_signals.py`.

### Fixed decisions

**F1 — The release.**
- **Version and metadata:**
  - version 0.1.0;
  - `pyproject.toml` gains the project URLs, a development status of "Alpha", and only the
    classifiers for the interpreters and systems the gates and the first-run test ran;
  - the README's "early development, do not rely on this for hostile code yet" stays.
- **The dependency set,** before the upload:
  1. a fresh resolve of the release wheel re-freezes `requirements/constraints.txt`, by
     GETTING_STARTED's regenerate procedure;
  2. both gates run on that set, the stdio end-to-end, signal and shutdown tests included;
  3. the published `mcp` range is the tested minor (P2).
- **The images are re-pinned once, before the upload.** The pins date from 2026-10-10, and
  get no upstream fixes until re-pinned. A re-pin of the same reference form is M1 S6's routine
  procedure, plus the M2 step 2 amendment (`setup` and `pytest -m network` on Linux).
- **No TestPyPI rehearsal.** With TestPyPI as an extra index, uv takes the first index's
  versions of every package, and TestPyPI holds stale `mcp`, `anyio` and `pydantic` entries.
  Resolving through it failed (§7). The rehearsal is the release candidate wheel (P6), plus a
  render check of the PyPI description (`twine check`).
- **Victor uploads** (`uv publish` with his token). A release is outward-facing and can't be
  taken back, so Claude never runs it.

> **Amendment 2026-11-09 (step 3, [`M4-step3-release.md`](M4-step3-release.md) §4, approved by Victor):**
> - The re-freeze moves only what the release wheel pulls in. The dev tools and what only they
>   need stay at their pins, so the gate's own checks don't change with the set they check.
> - Before the upload, `release_smoke.py smoke` fails on any package a fresh install gets that
>   neither gate ran.

**F2 — The release checklist** (a new `RELEASE.md` under `docs/`, and a smoke script).
- **Build from a clean export** (`git archive HEAD`), never the working tree, and give the
  sdist an explicit include list in `pyproject.toml`.
  - A build from today's tree shipped `.claude/settings.local.json`, a screenshot, a
    `.continue/` config and untracked plans (§7). The checklist lists the sdist's files and
    compares them to the include list.
- **Check the wheel holds** `native_helper.py`, `native_seccomp.py` and `seccomp-profile.json`
  (V13).
- **Install the wheel into a fresh `uv tool` environment** on Linux and Windows, then:
  - `import vestibule.server` (native reads its profile at import, so a missing file fails
    here);
  - `vestibule-mcp audit` (it prints the folder it read);
  - a stdio `initialize`, `tools/list` and one `tools/call` of `print(1)`;
  - the stdio end-to-end test against that install.
- **The gate,** green on both platforms on the re-frozen set; the repo's history checked for
  secrets; then the upload, then the same smoke test from PyPI.

**F3 — Leftover container run dirs after a hard kill** (M3 contract §1).
- **Today's layout** (`container.py:275`, `:482–486`):
  - on Windows, each run's dir is `%TEMP%\vestibule-run-*`, directly;
  - on POSIX, each backend makes a `vestibule-runs-*` parent under `/tmp`, and its run dirs
    sit inside it.

  A hard-killed server leaves its run dirs, and on POSIX their parent too. A startup sweep was
  rejected in M1 S7-D1 because it can't tell whose live run a dir belongs to.
- **The fix:**
  - **Run dirs carry their deadline in the name,** as each container's label does (S4-D3):
    spawn time + its timeout + the same 90 s. A run dir more than 60 s past its deadline is
    removed. A dir without a deadline, or with a garbled one, is never removed.
  - **A POSIX parent's name carries its owner's pid.** An empty parent whose pid is gone is
    removed. A reused pid only keeps it, the safe direction.
  - **When the sweep runs:** on first use and after each run, as the reaper does, but on its
    own: the reaper's pass returns early when no labeled container exists
    (`container.py:661–662`), which is the usual state after a hard kill. The directory scan
    runs in a thread, off the event loop.

> **Amendment 2026-11-08 (step 1, [`M4-step1-run-dir-sweep.md`](M4-step1-run-dir-sweep.md)
> §2.4, approved by Victor):** the sweep is the first step of the reaper's pass, ahead of its
> `ps` and that early return, not a task of its own. It still runs at first use and after
> each run, whatever the reaper finds. An empty parent whose pid is gone has no grace period;
> the plan's §5 states the one narrow case that costs.

**F4 — The README.**
- **Order:**
  1. the demo GIF and the why;
  2. **the boundary:** only code sent to `run_code` is sandboxed. An agent's own shell and file
     tools run on the host as usual. A sandbox-only session (the live-check kit's
     `--tools "" --allowedTools mcp__vestibule__run_code mcp__vestibule__read_workspace`) makes
     `run_code` the only way to run code;
  3. a quick start per platform (§3), with the Docker install method and its costs (P3);
  4. the client configs, and troubleshooting for a first run, `server/discover`'s warning
     included;
  5. what it protects and what it doesn't, linking `SECURITY.md`;
  6. the existing reference sections.
- **Configs** for Claude Code (one command), Cursor and Claude Desktop (JSON, with the absolute
  path and, where needed, `env.PATH`). Each is marked with what tested it, and only one used
  in a live session is called tested.
- **Uninstall:** the MCP entry, `uv tool uninstall vestibule-mcp`, `~/.vestibule`, and the two
  images. The commands chosen must not follow links out of the workspace (criterion 9).
- **Absolute URLs** for every link and image, because the README is also the PyPI page, where
  relative links break.
- Clone and dev install move to GETTING_STARTED, which already has the dev path.

**F5 — `SECURITY.md`'s outline.**
1. **What Vestibule is for:** the realistic agent risks it makes structurally impossible
   (exfiltration, destructive commands, resource exhaustion) **for code sent to `run_code`**,
   and what it isn't (a VM, a defense against kernel 0-days, a guard on an agent's own tools).
2. **The boundary, per backend:**
   - native's layers: namespaces, the jail, cgroups, and the seccomp allowlist from Docker's
     profile at moby `6fe7deb`, with `ptrace` allowed and `AF_VSOCK` refused;
   - the container's profile;
   - what `-degraded` means.
3. **Known residuals, each with its source:**
   - guest-planted symlinks in the workspace (S7-D2, N4);
   - the workspace has no disk cap (BUILD_LOG);
   - sandboxes run in the user manager's `app.slice`, outside the client's cgroup (README);
   - what a native guest can learn (uid and gid, the kernel, `/proc` CPU and memory, the host
     username in the mount table, the supplementary groups);
   - S7-D2's x32 point;
   - the container self-deadline a hostile guest can stop (S7-D5);
   - a container left Created after a client kill (7a);
   - the Windows shutdown race;
   - forced native without a user manager has no memory cap;
   - a root server refused;
   - the `docker` group's root-equivalence (P3).
4. **Supply chain:**
   - pinned images, and the re-pin tradeoff;
   - an unpinned override's warning;
   - one direct dependency (`mcp`, the tested minor), and about 25 transitive packages that
     every install resolves fresh.
5. **The audit log:** not tamper-proof (A12); its folder's permissions, Windows' inherited
   ACLs included; a pre-existing folder keeps its mode.
6. **Stderr's crash-path writers** (A11, step 4).
7. **Reporting a vulnerability** (P8), and the supported versions (0.1.x).

Every claim in it points to a test, a plan decision, or a stated residual.

> **Amendment 2026-11-09 (step 4, [`M4-step4-docs.md`](M4-step4-docs.md) §3 and V4, approved by Victor):**
> - `SECURITY.md` doesn't say "structurally impossible". It says what is out of a run's reach for
>   a run reported as `native` or `container`, short of a kernel or runtime bug, and then lists
>   what stays possible.
> - "About 25 transitive packages" is the measured count: 30 on Linux and 31 on Windows,
>   counting Vestibule and `mcp`.
> - Two additions: what hasn't been tested, and that no one independent has reviewed the code.
> - `read_workspace`'s check-then-open race is fixed, not listed
>   ([`M4-step4-workspace-race.md`](M4-step4-workspace-race.md)). What that fix leaves is listed.

---

## 3. The first-run path, as the README will show it (P1, P4, P5)

| Platform | Prerequisites | Commands | Expected |
|---|---|---|---|
| Linux with systemd (Debian, Fedora, Arch, WSL2) | uv, Claude Code; on a minimal install, `ca-certificates` and `dbus-user-session`, then a reboot | `uv tool install vestibule-mcp`, `vestibule-mcp setup`, `claude mcp add --scope user vestibule -- vestibule-mcp` | `ready …: isolation native` |
| Ubuntu 23.10+ | uv, Claude Code, Docker Engine from Docker's apt repo | the same three | `ready …: isolation container`, with the line saying why native isn't used |
| Windows | uv, Claude Code, Docker Desktop (Virtual Machine Platform turned on first) | `uv tool install vestibule-mcp`, `uv tool update-shell` and a new terminal, `vestibule-mcp setup`, `claude mcp add --scope user vestibule -- vestibule-mcp` | `ready …: isolation container` |
| macOS | the same | the same | untested for runs (P9) |

Then: "ask your agent to run `print(1)`".

> **Amendment 2026-11-08 (step 0, [`M4-step0-spike.md`](M4-step0-spike.md) A-1, B-1, B-2 and B-3, approved by Victor):** the Linux
> and Windows rows above now carry what step 0 measured.
> - A minimal Debian needs `ca-certificates` before `setup` can download, and the user-bus
>   fix takes effect only after a reboot.
> - On a clean Windows 11, Docker Desktop's recommended install needs the Virtual Machine
>   Platform feature turned on in an admin PowerShell, then a restart. That is part of
>   installing Docker, so it's outside P6's clock.

> **Amendment 2026-11-09 (step 4, [`M4-step4-docs.md`](M4-step4-docs.md) §2.4, approved by
> Victor):** the README words the Virtual Machine Platform line as the fix for the failure that
> was measured ("if the recommended install stops and asks for it"), not as a first step. It was
> one sample, and nothing re-runs it.

---

## 4. Time budgets and failure modes

- **Downloads,** at the test's 25 Mbit/s: the native trees are 123 MB, about 40 s. The two
  images are about as large, and with P4 a machine downloads one set, not both.
- **The first `uv tool install`** resolves about 25 packages, and may download a Python. Its
  time counts, and the test records it.
- **The readiness check** costs one selection probe: about 174 ms for native and about 1 s for
  a container (M3's live checks).
- **Docker installed but its daemon stopped:** the image step says so, and the readiness check
  prints selection's own message.
- **Docker installed but the user not in the `docker` group:** the pull is refused. `setup`
  says so, and exits 0 only if native was ready anyway.
- **uv on `PATH` only in a new terminal** (winget): the quick start says to open one.

---

## 5. Acceptance criteria

1. **The release.**
   - `vestibule-mcp` 0.1.0 is on PyPI, built from a clean export.
   - Its sdist holds only the include list's files.
   - F2's smoke test passes from the index on Linux and Windows.
2. **The first run** (P6), on the release candidate before the upload and confirmed from PyPI
   after it.
   - Each machine is within 2 minutes by the clock, at 25 Mbit/s, and ends in the agent's first
     result with the expected isolation: Debian `native`, Ubuntu with Docker `container`,
     Windows `container`.
   - Every command typed is one the README shows. The line speed and the interpreter uv chose
     are recorded.
3. **Setup** (P4, P5), on each machine:
   - it downloads only what that machine uses: no native trees on stock Ubuntu, no images
     where native is full;
   - its readiness line matches the isolation of the first tool call in Claude Code, and in one
     GUI client.
4. **Stock Ubuntu without Docker:** the first call and `setup`'s check are `Blocked:` with the
   AppArmor reason and the Docker fix, never a wrong isolation, and the message doesn't send
   the user back to `setup`. This closes N5's VM promise.
5. **The README's configs:** Claude Code's and Cursor's were used in live sessions, and so was
   Claude Desktop's if it's marked tested. Anything not tested says "untested".
6. **`SECURITY.md`** covers F5's outline, each claim has its source, and it is reviewed by
   `critic` and `verifier`.
7. **The demo GIF** shows a real session with its caption, in about 20 s, under 3 MB, and it
   renders on the PyPI page.
8. **F3's sweep:**
   - a run dir past its deadline and margin is removed, with no containers present, on Windows'
     flat layout and POSIX's parents;
   - an empty POSIX parent whose pid is gone is removed;
   - a live run's dir, an undated dir, a garbled one and a live server's parent are never
     removed.
9. **Uninstall:** after the README's uninstall steps on a test machine, nothing Vestibule made
   is left except what the section names. A sentinel folder that a symlink in the workspace
   points to survives.
10. **macOS** (P9): the manual job passes, and its run is recorded.
11. **The gate,** green on both platforms on the re-frozen dependency set, and
    `tests/test_docs.py`.

> **Amendment 2026-11-09 (step 4, [`M4-step4-docs.md`](M4-step4-docs.md) §4, approved by
> Victor):** in criterion 2, "every command typed is one the README shows" counts a page the
> README sends you to, for Docker's install alone.

---

## 6. Implementation order (each step ≈ one commit; each its own plan, `skeptic`-reviewed)

| Step | What | Rough size |
|---|---|---|
| 0 | Spike, no repo code (below) | — |
| 1 | F3: deadline-stamped run dirs, pid-stamped parents, their own sweep | src ~80, tests ~120 |
| 2 | P4 and P5: `setup`'s user-namespace check, its readiness check, its image step | src ~160, tests ~180 |
| 3 | F1 and F2: version, metadata, the sdist include list, the re-frozen constraints, the re-pin, `RELEASE.md` and the smoke script; P9's workflow | src ~20, script ~100 |
| 4 | Docs: the README per F4, `SECURITY.md` per F5, GETTING_STARTED; the demo GIF (Victor records) | docs |
| 5 | The first-run tests on the release candidate (§5), fixes if any; Victor uploads; the confirmation from PyPI; the docs pass; "M4 accepted" | — |

> **Amendment 2026-11-08 (step 0, [`M4-step0-spike.md`](M4-step0-spike.md) B-1, B-4 and B-5, approved by Victor):**
> - **Step 2** also adds two messages, with no new dependency. When `setup`'s download fails
>   certificate verification, it names `ca-certificates`. When the container probe fails
>   under snap Docker, the message names the snap and points to Docker's apt repo. Step 2's
>   plan decides how each is detected.
> - **Step 3's** smoke script runs Python 3.11 through 3.15, because uv 0.13.7 picks 3.15
>   where there's no usable system Python. F1's classifiers still claim only what ran.

**Step 0 measures:**
- `uv tool install` from a local wheel on Linux and Windows, native included, with Ubuntu's
  3.12 and Debian's 3.13 system Pythons as well as a uv-managed one;
- Claude Desktop's and Cursor's `PATH` on Windows;
- a Hyper-V VM with nested virtualization running Docker Desktop;
- stock Ubuntu 24.04's failure stage and message, and what snap Docker's failure looks like;
- whether Debian 13 minimal has a user bus;
- with admin rights, whether the uninstall commands follow a directory symlink.

Every step runs the full loop: plan, `skeptic`, sign-off, build, `gate`, `critic`, `verifier`,
docs.

---

## 7. Facts checked (2026-11-07)

**PyPI and the SDK:**
- `https://pypi.org/pypi/vestibule-mcp/json` returns 404, so the name is free.
- The newest mcp is 2.3.0, and the newest 1.x is 1.30.0 (2026-09-07). The SDK's "What's new in
  v2" says v2 "is the stable line" and that v1.x "keeps getting critical fixes and security
  patches". It also says the input schema is "advertised but never applied", and that 2.x
  serves the 2026-07-28 revision alongside the 2025 one.
- A fresh `uvx --from <wheel>` resolve installed mcp 1.30.0 and anyio 4.16.0. Its
  `mcp/server/stdio.py` and `lowlevel/server.py` are byte-identical to 1.28.1's, and stdio
  still reads with `async for line in stdin` (`verifier`).

**uv:**
- In WSL, `uvx` forwarded one SIGINT and one SIGTERM to the server, which logged its shutdown
  line. A SIGKILLed uv left the server running until stdin closed.
- On Windows the chain is `uvx.exe` → `uv.exe` → `vestibule-mcp.exe` → `python.exe` →
  `python.exe`; killing uv left the server until stdin closed (`verifier`).
- Resolving through TestPyPI as an extra index failed: uv's default `first-index` strategy
  took TestPyPI's stale `mcp` 0.8.0.dev0 (`verifier`).

**The sdist:** `uv build --sdist` from this working tree shipped `.claude/settings.local.json`,
`.claude/scheduled_tasks.lock`, a screenshot, `.continue/agents/new-config.yaml` and untracked
plans (109 entries). Those files are ignored only by the global git ignore and
`.git/info/exclude`, which hatchling doesn't read (`verifier`).

**Docker on Ubuntu (Docker's and Snapcraft's docs):**
- the snap reaches only non-hidden files in `$HOME`;
- its README says `no-new-privileges` stops containers from starting;
- the `docker` group "grants root-level privileges";
- rootless Docker's deb ships its AppArmor profile.

**Debian 13:** `dbus-user-session` is "optional" priority, and only recommended by
`libpam-systemd` (packaging metadata; a fresh install is step 0's check).

**macOS:** only monkeypatched `darwin` tests (`test_select.py:1078`, `test_rootfs.py:111`);
BUILD_LOG says "macOS untested".

**This machine and the repo:** `uv` and `uvx` are installed on Windows (winget), on Windows 11
Pro. The GitHub repo is public. M3's live checks ran Claude Code 2.1.319 on the 2025 protocol,
on both platforms.

**Not measured, so step 0 checks it:**
- whether PowerShell 5.1 deletes through a directory symlink (only a junction could be tested
  without admin, and it wasn't followed);
- GUI clients' `PATH`;
- nested virtualization;
- Debian minimal's user bus;
- Ubuntu's failure stage.

---

## 8. Review (`skeptic`, then `verifier`, 2026-11-07)

`skeptic`'s verdict on v1 was "sound with amendments". `verifier` checked the claims, adjusted
severities, and added the sdist's other leaked files and snap Docker's `no-new-privileges`
failure. Every finding is taken.

| # | Sev. (verifier) | Finding | In v2 |
|---|---|---|---|
| 1 | high → med | Under `uvx`, the setup fix every message names (`vestibule-mcp setup`) isn't on `PATH`; `setup` and a GUI client's server can resolve different versions; uvx between client and server was unmeasured. Verifier: uvx doesn't re-resolve per launch; on Linux it forwards SIGINT and SIGTERM | P1 now recommends `uv tool install` (the console script on `PATH`, one version, the same launch chain); `uvx` is option (2) with its costs |
| 2 | med | The release would ship a dependency set no gate ran (a fresh resolve gets mcp 1.30.0, anyio 4.16.0), and the shutdown path relies on SDK internals | F1: re-freeze, both gates on that set, the published range narrowed to the tested minor; F2's smoke adds a `tools/call` and the stdio end-to-end test |
| 3 | high | The demo's sandbox-only launch isn't what the README installs; an agent's own Bash runs on the host, and nothing said so | F4 states the boundary and shows the sandbox-only session; P7's caption; F5 §1 |
| 4 | med | The first-run tests came after an irreversible upload; Ubuntu's and Debian's system Pythons never ran | P6 and §6: the tests run on the release candidate first; step 0 adds those Pythons; F1's classifiers claim only what ran |
| 5 | med | The readiness check reports the terminal's environment, not the GUI client's (runtime found on `PATH`; `VESTIBULE_*` from the shell) | P5's wording and printed inputs; `env.PATH` in GUI configs; criterion 3 adds a GUI client; the `aclose()` and hard-exit rule |
| 6 | med | `setup` downloads what the machine won't use; the clock was tied to a fast line; uv's install was outside it; the fallback account may share Docker's store | P4's order (user-namespace check, then the images only if needed); P6's 25 Mbit/s cap, uv inside the clock, the empty-store check |
| 7 | med → med-high | P3's option (1) cost unweighed: the `docker` group is root-equivalent; snap Docker can't reach `~/.vestibule` or `/tmp`. Verifier added: the snap fails `no-new-privileges`; rootless Docker's deb ships its profile | P3 names Docker's apt repo, the group's cost and rootless; F5 records the group; the VM installs Docker as the README says |
| 8 | med | F3's layout was backwards (Windows is flat); POSIX parents leak; the reaper returns early with no containers; the scan must be off the loop | F3 rewritten: pid-stamped parents, its own pass, a thread; criterion 8 extended |
| 9 | med | TestPyPI as an index breaks resolution and adds risk. Verifier: resolution failed | F1 drops it; the rehearsal is the release candidate plus `twine check` |
| 10 | med → high | The sdist ships local, ignored-only-globally files. Verifier: confirmed by a build, with more leaked files | F2: build from `git archive`, an include list, the file list checked; criterion 1 |
| 11 | med → low (plausible at best) | Windows PowerShell 5.1's `Remove-Item -Recurse` may follow guest symlinks. Verifier: a junction wasn't followed; a symlink needs admin to test | Step 0 tests it with admin; criterion 9's sentinel makes uninstall able to fail |
| 12 | med | The GUI clients were never tested, and criterion 5 couldn't fail | P6 adds live Cursor and Claude Desktop sessions; criterion 5 requires PLAN.md's two clients |
| 13 | med (plausible) | Debian 13's expected `native` depends on the install flavour (`dbus-user-session`) | P6 names the minimal install; the prerequisites name the package if step 0 confirms |
| 14 | med (confirmed, low) | A macOS quick start for a platform where the server never ran | §3 marks it; P9 offers one manual macOS job |
| 15 | med | F5 overclaimed: no workspace disk cap, `app.slice`; "`mcp` only" hides about 25 packages | F5 §3 and §4 |
| 16 | low-med | The README is the PyPI page, where relative links and the GIF break | F4: absolute URLs; criterion 7 |
| 17 | low | P2 cited BUILD_LOG, not the signed S1-D1 or M3's "Later"; `server/discover`'s warning is a stranger's first log line | P2 amends S1-D1 by name; F4's troubleshooting |
| 18 | low | P8 and F1 assume a public repo | Checked: public. F2 adds the history check for secrets |
| 19 | low | The demo prompt may be refused | P7's fallback: the prompt-injection file |
