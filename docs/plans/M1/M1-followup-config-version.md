# M1 follow-up — config validation + server version

Status: **approved 2026-10-14** by Victor ("create a brief plan … then proceed with the 2
fixes"). Kept short on purpose; no `skeptic` round (small fix, user asked to proceed). The
diff still gets `critic` + `verifier` because it touches `src/`. **Built 2026-10-14**: gate
green, `critic` ×2 + `verifier` ×2, every finding fixed (§Review).

Fixes two known issues from `docs/BUILD_LOG.md`, both found in M1 and neither touching
isolation.

## 1. Config settings are not validated

**Today** (`config.py` `Limits.from_env`, `_i`): `int(v) if v and v.isdigit() else default`.
- A bad value (`VESTIBULE_MEM_MB=512m`, `-1`) is silently replaced by the default. That
  breaks golden rule 6: the user thinks they set a limit and didn't.
- `0` passes. `MAX_CONCURRENT=0` refuses every run with a misleading "retry shortly";
  `MAX_OUTPUT_BYTES=0` kills every run at its first byte.
- `²` passes `isdigit()` but not `int()`, so the server crashes at import and never starts.

Same class, added to scope: `VESTIBULE_WORKSPACE_RO=ture` (typo) is silently "off", which
makes the workspace writable when the user asked for read-only.

**Rule after the fix**
- Number settings: ASCII digits only, value ≥ 1. Surrounding whitespace is stripped.
  More than 18 significant digits is "too large" (added in review: a syntax cap checked
  before `int()`, so Python's adjustable 4300-digit limit never decides the verdict).
- On/off settings: `1/true/yes/on` or `0/false/no/off`, any case.
- Unset or blank (`""`, spaces) = not set = default (unchanged).
- String settings (`WORKSPACE`, `RUNTIME`, `BACKEND`, images) are unchanged; unknown
  runtime/backend values already get legible refusals at selection.
- Every invalid setting is reported, not just the first.

**What the server does with a bad config (decision).** It **starts** and refuses every tool
call with `Blocked: Vestibule's configuration is invalid: VESTIBULE_MEM_MB='512m' is not a
positive whole number; … Fix the setting in the MCP client's config for this server, then
restart it. Nothing was run.`, and logs the same line to stderr at startup.
- Why not refuse to start: the client only shows "failed to connect"; the reason is buried
  in a log the model can't read. A `Blocked:` message reaches the agent, which tells the
  user (golden rules 4 and 6; same pattern as a missing runtime).
- Why not start with defaults and warn: that's today's silent substitution again, since
  stderr isn't seen. Runs would happen under limits the user did not ask for.
- `read_workspace` is blocked too: one rule ("a misconfigured server runs nothing") is
  easier to explain than per-setting exceptions.
- The tool schema renders from the defaults while blocked (it needs some `max_timeout_s`);
  no call can use them.

**Code**
- `config.py`: `ConfigError(ValueError)` carrying `problems: list[str]`; `from_env`
  collects problems and raises once at the end.
- `server.py`: `_load_limits() -> (Limits, error | None)` catches `ConfigError`, logs it, and
  falls back to `Limits()`; module-level `LIMITS, CONFIG_ERROR = _load_limits()`;
  `call_tool` returns `_blocked(CONFIG_ERROR)` before dispatch.

## 2. The server reports the SDK's version

`server.py`: `Server("vestibule")` → `Server("vestibule", version=__version__)`. Without
`version=` the SDK sends its own package version (`1.28.1`); Vestibule is `0.0.1`
(`__init__.py`, matching `pyproject.toml`).

## Tests (`tests/test_config.py`, Docker-free)

- unset/blank ⇒ defaults; valid numbers (incl. whitespace) and on/off forms are used
- each bad number ⇒ `ConfigError` naming the variable and value: `512m`, `-1`, `0`, `²`,
  `٣` (a Unicode digit `int()` accepts), `1_000`, `1.5`, `+5`
- bad on/off (`ture`, `2`) ⇒ `ConfigError`; several bad settings ⇒ all reported
- `_load_limits` returns defaults + the message; `call_tool` blocks both tools and never
  reaches the backend selector
- version: `create_initialization_options().server_version == __version__`
- **real stdio** (the console script, no Docker needed): `VESTIBULE_MEM_MB=²` +
  `VESTIBULE_MAX_CONCURRENT=0` ⇒ the server starts, `initialize` reports `0.0.1`, `run_code`
  returns the `Blocked:` message naming both, stdout stays pure JSON-RPC, exit code 0 on
  stdin close. Reuses `StdioServer` from `test_stdio_e2e.py` (gains an `env` parameter).

Red first: on the old code, `²` crashes `import vestibule.server`, `512m` silently becomes
256, and the reported version is `1.28.1`.

## Docs

README Configuration paragraph (drop "does not validate them yet", state the rule and the
`Blocked:` behavior) and the `WORKSPACE_RO` row; BUILD_LOG (both issues closed, status row,
entry); HISTORY bullet.

## Out of scope

- Cross-field checks (e.g. `DEFAULT_TIMEOUT_S` > `MAX_TIMEOUT_S`; the server already clamps).
- Upper bounds. A value the runtime refuses (huge memory) already surfaces through the
  probe as `container-degraded` or `Blocked:`.

## Review

All findings were low; `verifier` confirmed each one before it was fixed.

| Round | Finding | Verdict | Fix |
|---|---|---|---|
| 1 | 5000 ASCII digits pass the syntax check, then `int()` raises a plain `ValueError` at import (startup crash again) | CONFIRMED | Caught and reported, with long values cut in the message (`_shown`); replaced in round 2 |
| 1 | `CONFIG_ERROR` comes from the dev shell at import, so a stray setting blocks `test_acceptance.py`'s in-process calls | CONFIRMED (narrower: acceptance only) | `make_tool` pins `CONFIG_ERROR` to `None` |
| 1 | The stdio test fails instead of skipping without the console script; importing `test_stdio_e2e` runs Docker probes (claimed ~30 s stall) | Missing-exe CONFIRMED; stall REFUTED (one call, under 1 s measured) | `skipif(not SERVER_EXE.exists())` |
| 2 | `test_validation.py`'s `None`-arguments test passes on the config message, not the path it covers | CONFIRMED | Pins `CONFIG_ERROR` to `None` |
| 2 | The round-1 fix depends on Python's adjustable digit limit (`PYTHONINTMAXSTRDIGITS=0` accepts 5000 digits), and its message is false for a long whole number | CONFIRMED | The 18-digit cap above, with a "too large" message; tests pass with the limit on or off |
| 2 | The stdio test's "workspace not created" check is vacuous: a bad config discards `VESTIBULE_WORKSPACE`, so the server's workspace is the default under home | CONFIRMED | The test points `USERPROFILE`/`HOME` at tmp and asserts that home stays empty |
