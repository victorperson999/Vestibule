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
.venv/bin/pip install -e ".[dev]"       # Windows: .venv\Scripts\pip install -e ".[dev]"
```

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

The definition of done for any change (`CLAUDE.md`) is a clean run of all three:

```bash
.venv/bin/python -m ruff check .        # Windows: .venv\Scripts\python -m ...
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q
```

- **Docker-marked tests** (`pytest -m docker`) need a running daemon and both pinned images.
  Anywhere else they *skip*, and `pytest -rs` shows why, including the exact pull commands.
  They never pull anything. `pytest -m "not docker"` runs only the Docker-free tests.
- `tests/test_acceptance.py` is the M1 exam: one test per acceptance criterion, run through
  the real tool handler against real Docker. It includes a memory bomb and a fork bomb, both
  bounded by the sandbox's own caps.
- `tests/test_stdio_e2e.py` starts the real `vestibule-mcp` from the venv and talks raw
  JSON-RPC to it, including shutting it down the way a client does.
- `tests/test_docs.py` fails when a doc link or a `docs/…` path goes stale.

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
