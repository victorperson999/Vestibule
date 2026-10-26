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

Then pull the two pinned sandbox images with the exact commands in the README's
**One-time setup**. Vestibule never pulls images itself.

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

- **Docker-marked tests** (`pytest -m docker`) need a running daemon and both pinned images.
  Anywhere else they *skip*, and `pytest -rs` shows why, including the exact pull commands.
  They never pull anything. `pytest -m "not docker"` runs only the Docker-free tests.
- **Native-marked tests** (`pytest -m native`, Linux only) run real native sandboxes. They
  need unprivileged user namespaces and the two trees `vestibule-mcp setup` makes in the
  default `~/.vestibule/rootfs`. Otherwise they skip with the reason, and they never download
  anything. The Linux gate allows no skips, so run `setup` once on a new dev host.
- `tests/test_acceptance.py` is the M1 exam: one test per acceptance criterion, run through
  the real tool handler against real Docker. It includes a memory bomb and a fork bomb, both
  bounded by the sandbox's own caps.
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
checks every byte against the pin, and unpacks them. Nothing in the server uses them yet:
native becomes reachable in M2 step 6.

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
- **After switching Docker Desktop's image store** (classic ↔ containerd): pull the images
  again. The two stores are separate.
- **Logs** go to the server's stderr, never stdout (stdout is the MCP channel). Your MCP
  client decides where stderr ends up.
- **Leftover containers**: `docker ps -a --filter label=vestibule.run=1`. Each one carries
  its own deadline. A running container normally ends itself at that deadline, and any
  later Vestibule run removes containers more than a minute past it, in whatever state
  (a container that never started, or one whose guest stopped its own timer, is only
  removed that way). `docker rm -f <name>` removes one at once.
- **A regression**: `docs/BUILD_LOG.md` maps each behavior to the step that built it, its
  guarding tests, and where to start looking.
