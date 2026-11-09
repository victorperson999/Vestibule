# Vestibule — developer guide

For working on Vestibule itself. To just *use* it, follow **One-time setup** in the
[README](../README.md). The original Milestone-0 scaffold walkthrough is archived at
[`archive/M0-scaffold-walkthrough.md`](archive/M0-scaffold-walkthrough.md).

## Dev install

You need Python ≥ 3.11 and, for the container backend and its tests, Docker (Docker Desktop
on Windows/macOS, running Linux containers).

```bash
git clone https://github.com/victorperson999/Vestibule.git && cd Vestibule
python -m venv .venv
.venv/bin/pip install -e ".[dev]" -c requirements/constraints.txt
# Windows: .venv\Scripts\pip install -e ".[dev]" -c requirements/constraints.txt
```

`requirements/constraints.txt` pins every package, transitive ones included, so every dev
venv is identical on every platform. Without it, pip picks the newest versions allowed by
`pyproject.toml`. Changing a pin is a deliberate commit; the file's header says how.

Then run `vestibule-mcp setup` once (the README's **One-time setup**). It gets what this machine
can use, and pulls the two pinned sandbox images where the container backend needs them. A tool
call never pulls anything.

The install is *editable*: a source change takes effect the next time the server process
starts. An agent session that is already running keeps the old code until you restart it.

## Register the server with an agent

**Claude Code:**

```bash
claude mcp add --scope user vestibule -- "$PWD/.venv/bin/vestibule-mcp"
# Windows: claude mcp add --scope user vestibule -- C:\path\to\Vestibule\.venv\Scripts\vestibule-mcp.exe
```

`--scope user` makes it available in every project. Without it, the default registers it
for the current folder only, which is handy for keeping a dev build out of your other
projects.

**Other MCP clients**, or to set `VESTIBULE_*` variables: edit the client's MCP config file
(for example Claude Desktop's `claude_desktop_config.json`, or a project's `.mcp.json` for
Claude Code). The variables are listed in the README's **Configuration** table.

```json
{
  "mcpServers": {
    "vestibule": {
      "command": "C:\\path\\to\\Vestibule\\.venv\\Scripts\\vestibule-mcp.exe",
      "env": {
        "VESTIBULE_WORKSPACE": "C:\\Users\\you\\vestibule-workspace",
        "VESTIBULE_MAX_TIMEOUT_S": "120"
      }
    }
  }
}
```

Settings are read once, when the agent starts the server, so restart it after any change.

## Run the checks

The definition of done for any change (`CLAUDE.md`) is a clean run of all four:

```bash
.venv/bin/python scripts/check_env.py   # Windows: .venv\Scripts\python ...
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q
```

`check_env.py` comes first: it fails if the venv differs from the constraints in any
package, so a stale or upgraded dependency can't quietly change what the tests run against.
Run them as your own user, not root: a server started as root refuses to run code, so every
test that selects a real backend fails there.

- **Docker-marked tests** (`pytest -m docker`) need a running daemon and both pinned images.
  Anywhere else they *skip*, and `pytest -rs` shows why, including the exact pull commands.
  They never pull anything. `pytest -m "not docker"` runs only the Docker-free tests.
- **Native-marked tests** (`pytest -m native`, Linux only) run real native sandboxes. They
  need unprivileged user namespaces, `libseccomp.so.2` (the native warden requires it since
  M2 step 5; `apt install libseccomp2`), and the two trees `vestibule-mcp setup` makes in the
  default `~/.vestibule/rootfs`. Otherwise they skip with the reason, and they never download
  anything. The Linux gate allows no skips, so run `setup` once on a new dev host.
  - The resource-limit tests also need a systemd user manager that delegates `cpu`, `memory`
    and `pids` (WSL with systemd enabled has one). Each test checks that it left no
    `vestibule-*.scope` unit behind.
- `tests/test_acceptance.py` is the M1 exam: one test per acceptance criterion, run through
  the real tool handler against real Docker. It includes a memory bomb and a fork bomb, both
  bounded by the sandbox's own caps.
- `tests/test_native_acceptance.py` is the M2 exam: criteria 1–13 of the native contract,
  run through the real tool handler with real native sandboxes. It runs on Linux and needs
  what the native-marked resource-limit tests need, the user manager included.
- `tests/test_audit_acceptance.py` is the M3 exam: the usage numbers and the audit records,
  run through the real tool handler with real backends and a real audit file. Its native
  tests need what the M2 exam needs, and its two container tests are Docker-marked.
- `tests/test_stdio_e2e.py` starts the real `vestibule-mcp` from the venv and talks raw
  JSON-RPC to it, including shutting it down the way a client does.
- `tests/test_docs.py` fails when a doc link or a `docs/…` path goes stale.

## Linux / WSL development

The native warden (M2) only runs on Linux. On Windows, the code stays in the Windows checkout
and the Linux checks run inside WSL Ubuntu against those same files. The Linux venvs live in
the WSL home directory, not in the repo:

```bash
# inside WSL, from the repo:
bash scripts/gate-linux.sh --setup   # first time, or after a constraints change: rebuild the venvs
bash scripts/gate-linux.sh           # check_env, ruff, mypy, and pytest on two Pythons
```

From Windows, the same script runs through `wsl`:

```text
PowerShell:  wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/gate-linux.sh
Git Bash:    MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- bash /mnt/c/<repo>/scripts/gate-linux.sh
```

- `--setup` needs `python3` with the `venv` module (Ubuntu: `sudo apt install python3.X-venv`
  once). It installs `uv` into `~/.venvs/tools` and uses it to fetch a standalone Python
  3.11, without sudo.
- It builds two venvs: `~/.venvs/vestibule` (the system `/usr/bin/python3`, which runs the
  full suite) and `~/.venvs/vestibule311` (3.11, the oldest supported version, which runs
  the Docker-free tests and the stdio end-to-end tests). `check_env.py` fails if either venv
  runs the wrong Python, or is bound to a different checkout than the one being checked.
- **Any skipped test fails the Linux gate**, apart from a short, commented list of
  host-dependent skips in the script. For the Docker tests, enable Docker Desktop's WSL
  integration for the distro, or install Docker in it.
- Each pytest run is stopped after 10 minutes, so a hang fails the gate instead of blocking
  it.
- **No cache is written into the repo:** ruff runs with `--no-cache`, and mypy's and Python's
  caches go to `~/.cache`.

### The native rootfs: `vestibule-mcp setup`

Native isolation runs each sandbox in an unpacked copy of the same digest-pinned images the
container backend uses. `setup` downloads them straight from Docker Hub (no Docker needed),
checks every byte against the pin, and unpacks them. Every native run uses them, and the
server never downloads them itself. Until `setup` has run, native isn't available: under
`auto` a working container is used instead, and otherwise the call is `Blocked:` with the
exact command.

`setup` skips the trees where native can't start (user namespaces refused, a root user) or isn't
wanted (`VESTIBULE_BACKEND=container` or `naive`). Then it runs the server's own selection once.
If the answer isn't full `native`, it pulls whichever of the two images are missing and checks
again. It exits 0 when the final check says ready, and prints that answer plus what can differ for
a client started another way.

```bash
# inside WSL, with a dev venv:
~/.venvs/vestibule/bin/vestibule-mcp setup
```

- **Size:** about 123 MB to download and 357 MB on disk, in `~/.vestibule/rootfs` (about
  10 s here). A second run says "already present" and doesn't touch the network.
- **Settings:** it reads the same `VESTIBULE_*` settings as the server. If the client's MCP
  config sets `VESTIBULE_ROOTFS_DIR` or `VESTIBULE_IMAGE_*`, set them for setup too
  (`VESTIBULE_ROOTFS_DIR=… vestibule-mcp setup`). Setup prints the directory and images it
  used.
- **Keep the directory on the Linux disk** (under `~`), not under `/mnt/c`: symlinks, modes
  and letter case behave differently there.
- **It refuses a directory that overlaps the workspace,** and it never touches anything in
  the directory except its own entries. To start over, delete the directory.
- **Tests:** the ones that fetch the real images are marked `network`; the comparison with
  `docker export` is marked `docker` too. The Linux gate runs both.

## Troubleshooting

- **`Blocked: …`**: the message names the fix and says what to do next.
  - *Retry*: the problem is outside Vestibule, such as Docker not running, an image
    missing, or too many runs at once. Fix it and call again. A failed setup check is
    re-checked at most every 30 s, and the message says when.
  - *Restart*: a config or `PATH` problem. Both are read once, when the agent starts the
    server.
- **`isolation: container-degraded (…)`**: the run was still containerized, but the
  detail names the resource limits that were not applied or could not be verified.
- **`isolation: native-degraded (…)`**: every namespace, the jail and the seccomp filter
  held, but the detail names the limits that weren't applied and, when no scope was made,
  why (for example "limits not applied: cpu, memory, pids — no systemd user bus").
  - *No user bus:* the server needs a systemd user manager it can reach. Check with
    `systemctl --user status`; on WSL, enable systemd for the distro.
  - *A limit missing on its own* (often `cpu`): the user manager doesn't delegate that
    controller. `cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers`
    must list `cpu memory pids`; systemd 252 and later delegate all three by default.
  - Restart the server after fixing it. Under `VESTIBULE_BACKEND=auto`, a full container, if
    one works, is chosen over native-degraded anyway.
- **`audit: not recorded (…)`** or **`audit: not confirmed (…)`**, just above `isolation:`:
  the run still happened; only its record in the audit log is missing or late. The server's
  log has a warning too.
  - *not recorded*: the record was lost, and the reason says why: the folder couldn't be
    created, the disk is full, or the file was deleted while it was written. Check that
    `VESTIBULE_AUDIT_DIR` (default `~/.vestibule/audit`) is writable and has space. The next
    call tries again.
  - *not confirmed (the disk didn't answer within 2 s)*: a slow or network drive. The record
    may still land.
  - *could not find the home directory; set VESTIBULE_AUDIT_DIR*: the default folder lives
    under your home directory, and this machine has none. Set an absolute path.
- **`Blocked: … the audit directory (…) and the workspace (…) overlap`**, or the rootfs
  directory: sandboxed code could reach the records. Point one of the two settings somewhere
  else, then restart the server. `VESTIBULE_WORKSPACE=~/.vestibule` does this with the default
  audit folder.
- **`vestibule-mcp audit` says `no records`**: the command reads `VESTIBULE_AUDIT_DIR` from your
  shell (default `~/.vestibule/audit`), and the first line names the folder it read. If your MCP
  config sets the setting, set the same value for the command.
  - *`… unreadable lines skipped`*: a record torn by a full disk or a crash, or a hand edit.
    Every other record is still shown.
  - *`warning: the audit directory (…) and the workspace (…) overlap`*: sandboxed code can write
    to the folder it read, so those records may be forged. The server refuses that setting.
  - *Escapes such as `\u65e5` in place of characters*: the output isn't UTF-8 (a Windows pipe,
    or a terminal in a legacy charset), so everything outside ASCII is printed escaped. Control
    and invisible formatting characters are printed escaped everywhere.
- **`usage: … not measured: …`**: the result names the numbers it lacks, and why.
  - *not readable from outside a container*: container runs report only `wall`. Anything
    more could only come from the guest's own output, which it can fake.
  - *the naive backend has no sandbox to measure*: the dev-only backend.
  - *this run had no cgroup*: the native run wasn't placed in a cgroup of its own. Its
    `native-degraded` detail names the limits that weren't applied, and why when no scope
    could be made (above).
  - *the sandbox's hard controls weren't confirmed*: the result also says `isolation: none`
    and names those controls. They are what keeps the guest away from the channel the
    numbers travel on, so none are shown.
  - *the sandbox didn't report its counters*: usually a timed-out run whose sandbox took more
    than 1 s to empty after the stop. A low `VESTIBULE_CPU_PCT` (5 or less) makes that
    likely for a fork bomb, since throttled processes exit slowly.
  - *Linux 5.19+ needed* / *Linux 6.1+ needed*: the kernel has no `memory.peak` or
    `pids.peak`.
  - *not readable*: that counter's controller isn't enabled, or its file couldn't be read.
- **Leftover native sandboxes**: `systemctl --user list-units 'vestibule-*'` lists each run's
  scope. It should be empty when no run is in flight; systemd removes a scope when its run
  ends, and `systemctl --user stop <unit>` stops one at once.
- **After switching Docker Desktop's image store** (classic ↔ containerd): pull the images
  again. The two stores are separate.
- **Logs** go to the server's stderr, never stdout (stdout is the MCP channel). Your MCP
  client decides where stderr ends up.
  - *`stderr was not being read: dropped N log line(s)`*: the client stopped reading the
    server's log for a while, so N lines were thrown away instead of making the server wait.
    Nothing else was affected: tool calls kept answering, and the audit log is separate.
  - *`<logger>: a log call failed to format (…) at <file>:<line>`*: a log call, Vestibule's or
    a library's, had arguments that didn't fit its message. That line is lost; the note says
    where it came from.
  - *`Failed to validate request: 31 validation errors for ClientRequest`*, naming
    `server/discover`, once as a session starts: Claude Code first tries a newer protocol's
    opening request, which the MCP SDK Vestibule uses doesn't know. The SDK answers
    it with an error and logs the whole validation failure, and Claude Code then connects the
    usual way. Harmless, even where Claude Code's own MCP log labels it an error.
- **After you interrupt a call** (Ctrl+C in Claude Code), Claude Code's MCP log says the
  connection dropped (`Received a response for an unknown message ID … Request cancelled`):
  the MCP SDK answers the cancelled call, and Claude Code doesn't expect that answer. The run
  was stopped and cleaned up, and its audit record says `cancelled`. Whether a later call in
  the same session still works hasn't been tested; if one fails, restart the session.
- **Leftover containers**: `docker ps -a --filter label=vestibule.run=1`. Each one carries
  its own deadline. A running container normally ends itself at that deadline, and any
  later Vestibule run removes containers more than a minute past it, in whatever state
  (a container that never started, or one whose guest stopped its own timer, is only
  removed that way). `docker rm -f <name>` removes one at once.
- **A regression**: `docs/BUILD_LOG.md` maps each behavior to the step that built it, its
  guarding tests, and where to start looking.
