# M4 step 2 — one `setup` for every platform, ending in a readiness check (P4, P5)

Status: **v2, signed off 2026-11-08 with T1–T4 and T6 as written, and built by 2026-11-09 (§12).** v1 was reviewed by `skeptic` (9 findings: 3 medium,
6 low; verdict "sound with amendments"). `verifier` checked 9 claims about the existing code and
logs: all confirmed, one only in part (§11). 8 findings are taken below; 1 is declined, with the
reason.

Contract: [`M4-adoption.md`](M4-adoption.md), P4, P5, §3, §4 and §6's step 0 amendment (the two
messages). Acceptance criteria 3 and 4 are finally judged on the VMs in step 5; this step builds
what they test.

## 1. The problem

`vestibule-mcp setup` today (`rootfs.py:102–157`):
- **On Linux it always downloads both trees,** 123 MB, even where user namespaces are refused
  and native can never start. Stock Ubuntu 24.04 did exactly that in step 0, then every call was
  `Blocked:`.
- **On Windows and macOS it does nothing.** The user must find two `docker pull` lines in the
  README.
- **It never says whether the machine works.** The first tool call is the only check, and its
  answer lands in the agent's session, not in the terminal of the person installing.
- **Its failures don't name the fix.** On a minimal Debian it stops at
  `CERTIFICATE_VERIFY_FAILED` (step 0, B-1). Under snap Docker the probe's message doesn't name
  the snap (B-5).

One more, in selection: after this step, stock Ubuntu has no trees, so `_try_native` would answer
"no native rootfs; run `setup`". That sends the user in a circle.

## 2. The fix

### 2.1 The flow

A new module, `src/vestibule/setup_cli.py` (as `audit_cli.py` is for `audit`), lazy-imported from
`server.run()`. It can't live in `rootfs.py`, because `select.py` imports `rootfs`.

```
main():
  logging for "vestibule" at ERROR       # §2.5: the report below is the output
  Limits.from_env()                      # ConfigError: print every problem, exit 1, nothing done
  _native_step(limits)                   # P4.1: Linux only
  asyncio.run(_ready_steps(limits))      # P4.2, P4.3
  KeyboardInterrupt: "interrupted", exit 130

_ready_steps(limits):
  first = _check(limits)                 # P5: the server's real selection
  if _wants_images(limits, first):
      pulled, failed = _image_step(limits)     # P4.3
  final = _check(limits) if pulled else first  # "then checks again"
  return _report(limits, final, failed)  # prints; the exit code
  finally: the hard-exit rule            # §2.3
```

### 2.2 The native step and the user-namespace check (P4.1)

```python
def _native_step(limits):
    if sys.platform != "linux":
        say("native isolation is Linux-only, so this machine uses a container runtime"); return
    if limits.backend not in ("auto", "native"):
        say(f"VESTIBULE_BACKEND={limits.backend!r}: skipping the native trees"); return
    refused = root_refusal() or userns_problem()
    if refused:
        say(f"not downloading the native trees (about 123 MB): {refused}"); return
    rootfs.install(limits)       # prints its own progress and failures; §2.5 decides the exit code
```

`root_refusal()` is `_select`'s root check, moved into a function with its message unchanged:
selection always refuses root, so the trees would be 123 MB nobody can use.

`userns_problem()` lives in `select.py`, beside `_read_sysctls` and `_native_hint`, and selection
uses it too (§2.6). It returns why native sandboxes can't start here, or `None`.

- **It does what the helper does first** (`native_helper.py:111–116`): read the uid and gid, then
  `unshare(CLONE_NEWUSER | CLONE_NEWPID)`, then write `setgroups`, `uid_map` and `gid_map`.
  **The writes are the test.** On stock Ubuntu `unshare` succeeds and the first write fails with
  `[Errno 13] Permission denied: '/proc/self/setgroups'` (step 0's log).
- **It runs as a child process,** `sys.executable -I -B -c <probe>`, stdin and stdout to
  `DEVNULL`, 5 s bound. `unshare(CLONE_NEWUSER)` refuses a threaded caller and the server has
  threads, and a `fork` from a threaded server is what golden rule 3's child model avoids.
- **Only a definite refusal counts.** The child exits 77 only when one of those four calls raises
  `OSError`, and prints the error. Anything else (a crash, no `ctypes`, the timeout, a failure
  to spawn) returns `None`, and setup downloads as it does today. An unclear answer must never
  cost a host its native isolation.
- **The message** is `native sandboxes can't start on this host (<error>). <hint>`, where the hint
  is the sysctl half of `_native_hint`, split out as `_namespace_hint(sysctls)`, so Ubuntu's
  AppArmor line and the `max_user_namespaces=0` line have one wording.

`rootfs.setup_main()` becomes `rootfs.install(limits) -> int`: it loses the config parsing and the
"Linux-only" message, which `setup_cli` now owns. Its checks on `VESTIBULE_ROOTFS_DIR`, the overlap
check and everything after stay. Its return value is printed-and-ignored (§2.5).

### 2.3 The readiness check (P5)

```python
async def _check(limits) -> Selection | RunRefusedError:
    selector = BackendSelector()         # fresh: a selector caches its verdict and its failure
    try:
        selection = await selector.get(limits)
        await selection.warden.drain(_DRAIN_S)   # the probe's finisher, so the run dir is gone
        return selection
    except RunRefusedError as e:
        return e
    except Exception as e:               # a workspace mkdir refused, say: a line, not a traceback
        return RunRefusedError(f"the readiness check failed: {e}")
    finally:
        await selector.aclose()
```

`_ready_steps` wraps the steps in P5's hard-exit rule:

```python
async def _ready_steps(limits) -> int:
    code = 1
    try:
        code = await _steps(limits)
    except asyncio.CancelledError:       # Ctrl+C
        code = 130
        raise
    except Exception as e:               # a hard exit below would otherwise hide it
        say(f"vestibule setup: failed: {e!r}")
    finally:
        if _other_live_tasks():          # never into asyncio.run with a task left
            sys.stderr.flush()
            os._exit(code)
    return code
```

That is `server.py:527–535`'s rule, for the loop-close hang found in M1 step 5.

### 2.4 The image step (P4.3)

```python
def _wants_images(limits, first) -> bool:
    if limits.backend not in ("auto", "container"): return False
    if isinstance(first, _RestartRequired): return False  # a setting, root, or no runtime
    return isinstance(first, RunRefusedError) or first.verdict != "native"
```

```python
async def _image_step(limits) -> tuple[bool, list[str]]:
    try:
        runtime = await BackendSelector()._resolve_runtime(limits)
    except _RestartRequired:               # none installed: nothing to say, the check says it
        return False, []
    except RunRefusedError as e:           # §4: daemon stopped, or not in the docker group
        say(f"can't get the sandbox images yet: {e}")
        return False, [limits.image_python, limits.image_node]   # unconfirmed counts as failed
    backend = ContainerBackend(runtime)
    pulled, failed = False, []
    for ref in (limits.image_python, limits.image_node):
        present = await backend.image_present(ref)
        if present: continue
        if present is None:                # no answer: a pull would hang the same way
            say(f"{runtime} didn't answer about {ref}, so it wasn't pulled")
            failed.append(ref); continue
        say(f"pulling {ref} with {runtime}")
        if await backend.pull_image(ref) == 0: pulled = True
        else: failed.append(ref); say(f"  could not pull {ref}; the reason is above")
    return pulled, failed
```

Two small methods go on `ContainerBackend`, beside `_preflight_image`:
- `image_present(ref) -> bool | None`: the same bounded `image inspect`. `True` on exit 0, `False`
  on a non-zero exit, `None` when `_cli_status` got no answer (a timeout or a failed spawn).
- `pull_image(ref)`: `<runtime> pull <ref>`, stdin `DEVNULL`, **`stdout=2` and `stderr=2`**, so
  the runtime's progress reaches the user and stdout stays empty (golden rule 1). On
  `CancelledError` it kills the child. Only `setup` calls it, never a tool call (D2).

### 2.5 The report and the exit code

The report's first line is P5's exact words, followed by what `Selection.detail` says when it says
anything. That is the only place left for a degraded reason, since T3 hides the log line.

```
ready in this terminal's environment: isolation native-degraded (limits not applied: … no systemd user bus)
  native isn't used: <selection's own reason>          (only when it applies, §2.6)
  a client started another way can differ in these:
  container runtime on this PATH: /usr/bin/docker      (or "none")
  VESTIBULE_ROOTFS_DIR='/data/rootfs'                  (each one set; or "no VESTIBULE_* settings")
```

or `Blocked: <selection's exact message>` and the same input lines.

| Final check | Exit |
|---|---|
| A `Selection` | 0 |
| A `Selection` of the container tier, but an image wasn't confirmed or pulled | 1, with the line `not ready: isolation X, but these images are missing or unconfirmed: <refs>` in place of `ready …` (T1) |
| `RunRefusedError` | 1 |

A failed tree download doesn't change the exit code on its own: the final check decides (P4).
A native selection can't exist with a tree missing (`_try_native` needs both).

- **Log lines:** `setup` sets the `vestibule` logger to `ERROR` (T3). Selection logs at INFO and
  WARNING for an operator's log file; here, the first check's would be printed and then repeated
  by the second. Crash paths still log at ERROR.
- **S6-D2's "never silent":** an unpinned image can only come from an override, and overrides
  are listed, so a `VESTIBULE_IMAGE_*` line gets `(not digest-pinned)` from `_PINNED_REF`.
- **Values are printed with `repr`,** so a control character in one can't drive the terminal.

### 2.6 What changes in selection (`select.py`)

- **`_try_native`:** when a tree is missing, it asks `await asyncio.to_thread(userns_problem)`
  first. If refused, it raises `RunRefusedError(<that reason>)`, with no `setup` in it. Otherwise
  the message is today's. The check costs 14 ms, only when a tree is missing, at most once per
  30 s cooldown.
- **`Selection.why_not_native: str | None = None`,** set in `_select_auto_linux` when the container
  wins: `str(native_err)`, or a native-degraded loser's `detail`. §3's Ubuntu row needs that line,
  and P5 wants selection's own wording. Nothing in the server reads it.
- **The snap hint.** In `_select_container`'s last refusal, when `shutil.which(runtime)` starts
  with `/snap/`, the message gains: `. <path> is the snap package. A snap can't start a container
  with no-new-privileges, which every Vestibule run sets. Install Docker Engine from Docker's apt
  repository: https://docs.docker.com/engine/install/ubuntu/`. Step 0's log has
  `== docker: /snap/bin/docker`. Don't `realpath` it: that link points at `/usr/bin/snap`.

### 2.7 The certificate hint (`registry.py`)

In `Registry._open`'s `except OSError`: if `getattr(e, "reason", e)` is an
`ssl.SSLCertVerificationError`, the message gains: `. This machine may have no trusted
certificates yet: install the ca-certificates package and run setup again. Behind a proxy that
inspects TLS, add its certificate to the system's trusted store`. An exact type, no string
matching. `_open` is the one place: a token request comes after the registry's own TLS succeeded.

### 2.8 Small corrections this makes necessary

- `container.py:818–819`: "Vestibule never pulls images itself" is no longer true of `setup`.
  It becomes "a tool call never pulls images". The `docker pull <ref>` command stays, and the
  tests that pin it.
- `server.run()` calls `setup_cli.main()`; the now-unused `rootfs` import goes.
- `rootfs.py`'s docstring and the two "setup_main returns first" comments.

## 3. What a user sees

**Windows, first run:**
```
vestibule setup: native isolation is Linux-only, so this machine uses a container runtime
vestibule setup: checking what this machine can run
vestibule setup: pulling docker.io/library/python@sha256:… with docker
…docker's own progress…
vestibule setup: pulling docker.io/library/node@sha256:… with docker
…
vestibule setup: checking again
ready in this terminal's environment: isolation container
  a client started another way can differ in these:
  container runtime on this PATH: C:\Program Files\Docker\Docker\resources\bin\docker.EXE
  no VESTIBULE_* settings
```

**Stock Ubuntu, no Docker** (criterion 4, exit 1):
```
vestibule setup: not downloading the native trees (about 123 MB): native sandboxes can't start on
this host ([Errno 13] Permission denied: '/proc/self/setgroups'). Ubuntu restricts unprivileged
user namespaces on this host, the likely cause; lifting that needs root, so use the container backend
vestibule setup: checking what this machine can run
Blocked: no isolating backend is available — native: native sandboxes can't start on this host (…).
Ubuntu restricts …; container: no usable container runtime (docker: not installed; podman: not
installed). Install Docker (or Podman); …
```

**Ubuntu with Docker's apt packages:** the same first line, two pulls, then `ready …: isolation
container` and `native isn't used: native sandboxes can't start on this host (…)`.

**Debian with a working bus:** the trees' progress, then `ready …: isolation native`. No images.

## 4. Readings of the contract, and where this departs from its words

For Victor's sign-off:

- **T1 — An image the machine needs that wasn't confirmed or pulled exits 1, even if the check says
  `container`.** P4 says exit 0 "whatever an optional step skipped". The probe uses only the python
  image, so with node's pull failed the check would say `container` while every node run is
  `Blocked:`. That's a failed step the machine needs, not an optional skip, so `setup` says `not
  ready`. A runtime that stops answering during the image step counts the same. On a host where the
  final verdict is native or native-degraded, a failed pull changes nothing (§4 of the contract).
- **T2 — It pulls only what's missing.** P4 says it "pulls the two pinned images". Each is checked
  with `image inspect` first. A second `setup` then touches no network on any platform, which is
  what the trees already do, and needs no daemon-side pull of a present digest (1.0 s measured).
- **T3 — `setup` shows its report, not the server's log lines.** See §2.5.
- **T4 — Nothing bounds `docker pull` once it starts.** On a slow line a pull really takes minutes,
  its progress is on screen, and Ctrl+C ends it. A runtime that doesn't answer the bounded `image
  inspect` never gets a pull. Every other wait in `setup` is bounded.
- **T5 — The README keeps its two `docker pull` lines until step 4.** `test_pins.py` checks them,
  and step 4 rewrites that section. This step changes the comments around them.
- **T6 — One live check on the real Ubuntu VM, with Victor's help** (§7.1). It needs one command in
  an admin PowerShell to boot the VM. Without it, the real AppArmor path waits for step 5.

Other readings, stated:
- **"Then checks again" means after a pull.** With nothing pulled, the machine hasn't changed, and
  the first answer stands.
- **`VESTIBULE_BACKEND` narrows "what this machine needs":** `container` skips the trees, `native`
  skips the images, `naive` skips both and reports `ready …: isolation none (explicitly
  configured, dev-only)`.
- **A refusal only a config change or a restart can fix never triggers a pull** (`_RestartRequired`:
  a server run as root, an unknown `VESTIBULE_BACKEND`).

## 5. Edge cases

| Case | What happens |
|---|---|
| A second run, nothing changed | "already present", two local `image inspect`s at most, no network |
| The check finds Docker's daemon stopped (§4 of the contract) | The image step says so with selection's words; the final `Blocked:` says how to start it; exit 1 |
| `first` is `container`, node's image is missing, then `docker version` stops answering | Both refs count as unconfirmed; T1: `not ready`, exit 1 |
| `image inspect` gets no answer | No pull; that ref counts as unconfirmed |
| Snap Docker | Pulls, then the real probe fails with the snap hint; exit 1 (§10) |
| Not in the `docker` group | `docker version` exits non-zero: the same path, with Docker's own "permission denied" line; exit 0 only if native was ready |
| `first` is native-degraded and Docker works | Images pulled, checked again, `container` wins by N5 |
| `first` is native-degraded and the pull fails | Final stays native-degraded, exit 0 |
| Python pulled, node failed, check says `container` | T1: `not ready`, exit 1 |
| Both pulls fail | `pulled` is False, the first answer (`Blocked:` with the pull command) stands, exit 1 |
| Trees fail (no CA certs), Docker works | The hint is printed, images pulled, `ready …: isolation container`, exit 0, with `native isn't used: no native rootfs …` |
| Ctrl+C during the trees | `rootfs.install`'s `finally`s run; "interrupted"; exit 130 |
| Ctrl+C during a check or a pull | The main task is cancelled; `pull_image` kills its child; `aclose` runs; the hard-exit rule; exit 130 |
| The two `VESTIBULE_IMAGE_*` are the same ref | The second `image_present` sees the first pull |
| Run as root | No download; the final check says why it's refused; exit 1 |
| An unexpected exception in a step | Printed; exit 1; never reported as a Ctrl+C |
| A live server on the same machine | Nothing shared: the rootfs lock covers only the trees, each probe has its own names |
| A userns check that hangs | 5 s bound, `None`, downloads as today |
| `stderr` a pipe, not a terminal | Docker prints plain lines to it; nothing reaches stdout |

## 6. Facts measured for this plan (2026-11-08)

- **The check, in WSL, on 3.14 and 3.11:** 14 ms median, 18 ms worst, exit 0. A real refusal, with
  `max_user_namespaces=0` set inside a `unshare -Ur`, exits 77 with `[Errno 28] No space left on
  device: 'unshare'`. **A bug the spike caught:** reading the uid after `unshare` gets 65534 and
  the write fails, a false refusal on a host where native works. The helper reads it before;
  test 11 pins that.
- **A standalone selection:** Windows `container` in 0.95 s, 3 live tasks after `get`, 0 after a
  0.19 s drain; `aclose` clean; `asyncio.run` returned at once. WSL `native` 0.13 s, `container`
  0.68 s, same result.
- **`docker pull` under asyncio with `stdout=2, stderr=2`, on Windows:** exit 0, 0 bytes on stdout,
  Docker's lines on stderr. `image inspect`: 0.14 s.
- **From step 0's logs:** the stock-Ubuntu error text above; `== docker: /snap/bin/docker`;
  `exec /sbin/docker-init: operation not permitted`; the Debian `CERTIFICATE_VERIFY_FAILED` line.

## 7. Tests

New `tests/test_setup_cli.py`, Docker-free unless marked. The flow tests stand in at
`BackendSelector._select` (not `.get`, so a reused selector's cache would be caught),
`select.userns_problem`, `rootfs.install`, `select._version_check`, and `ContainerBackend`'s two
new methods. Every test asserts `capsys…out == ""`. An autouse fixture restores the `vestibule`
logger's level, because `main` changes it in-process and `test_shutdown.py` needs its WARNINGs.

| # | Test | Would fail if |
|---|---|---|
| 1 | A table of flows: platform, backend, userns answer, first result, images present, pull results, second result → exit code, `install` called or not, refs pulled, number of checks, key output words. Rows: full native; no userns with Docker; no userns no Docker; root; native-degraded with Docker; native-degraded without Docker, whose line must carry `(… no systemd user bus)`; Windows missing images; Windows present; daemon stopped; `first` container then the daemon stops; one pull fails; `image inspect` no answer; native-degraded and pull refused; trees fail and container works; `backend=container`, `native`, `naive`; `_RestartRequired` | Any branch of §2.1–§2.5 changes |
| 2 | An invalid setting: every problem listed, exit 1, no check, on every platform (replaces `test_setup_reads_its_own_settings`) | Config errors carry on |
| 3 | The real `_check` drains the winner and closes the selector (spies), on success, refusal and an unexpected exception | Either call is dropped |
| 4 | A task left live → `os._exit(code)` with the right code; none left → a normal return; an unexpected exception is printed and exits 1, not 130 | The hard-exit rule is dropped, or a crash reads as a Ctrl+C |
| 5 | Cancelled mid-check: `aclose` ran, exit code 130 | Ctrl+C skips the close |
| 6 | The input lines: runtime path or none, blank settings left out, `(not digest-pinned)`, `repr` | §2.5's lines change |
| 7 | The logger is at ERROR after `main` | Log lines come back |
| 8 | Real console script, invalid setting: exit 1 on every platform, stdout empty (extends `test_setup_does_not_print_…`) | The entry point or the output stream changes |

In the existing files:

| # | File | Test | Would fail if |
|---|---|---|---|
| 9 | `test_select.py` | `userns_problem` over a stubbed child: exit 0, 77, 1, timeout, spawn failure → None, reason, None, None, None; the hint per sysctl | An unclear answer starts refusing |
| 10 | `test_select.py` | Real, Linux: `unshare -Ur` + `max_user_namespaces=0` → the reason and its hint | The refusal path doesn't work for real |
| 10a | `test_select.py` | Real probe source, Linux, with `builtins.open` wrapped to raise `PermissionError` for `/proc/self/setgroups`, as stock Ubuntu does: exit 77 and that path in the message | The writes leave the probe, or leave its `try` |
| 11 | `test_native_select.py` | Real, Linux: `userns_problem() is None` on this host | The spike's uid bug comes back |
| 12 | `test_native_select.py` | Tree missing + refused → reason, and no `setup` in it; tree missing + not refused → today's message (existing test) | The circle comes back |
| 13 | `test_select.py` | `why_not_native`: set from a refusal, set from a degraded loser, None for a native winner and for forced container | §3's line goes |
| 14 | `test_select.py` | The snap hint for `/snap/bin/docker`, none for `/usr/bin/docker`; on the real last-refusal path | B-5's message regresses |
| 15 | `test_registry.py` | A `URLError(SSLCertVerificationError)` names `ca-certificates`; a plain `URLError` doesn't | The hint is dropped or fires on everything |
| 16 | `test_lifecycle.py` | `pull_image`'s argv and stdio; a spawn failure; a cancel kills the child. `image_present` on 0, non-zero and no answer | The stream or the kill changes |
| 17 | `test_container.py`, `docker` + `network` | The real `pull_image` of a present pinned image, in a child process with stdout captured: exit 0, stdout empty | Real Docker writes to stdout |
| 18 | `test_stdio_e2e.py` | **Criterion 3 in the small:** the real console script's `setup` (real trees or Docker), exit 0; its `ready …: isolation X` equals the first real `run_code`'s `isolation: X`; no container or run dir left | The check drifts from what a call gets |

`test_rootfs.py`: `rootfs.setup_main()` calls become `_setup()`, one local line calling
`rootfs.install(Limits.from_env())`. `test_setup_reads_its_own_settings` moves to test 2, because
`from_env` raises before `install` is reached. `test_setup_on_another_os_says_linux_only` is
replaced by test 1's Windows rows. `test_setup_runs_the_setup_command` patches `setup_cli.main`.

**Red first:** tests 12, 14 and 15 are the behaviors a user would hit today. Write them first and
see them fail on the current code.

**Mutants (foreground, byte-for-byte restore, named tests, green control first):** the userns
answer ignored in setup; ignored in selection; exit 1 counted as refused; the uid read after
`unshare`; the probe's writes removed; the probe's write errors not caught; the root check dropped
from the native step; one selector reused; the second check dropped; `aclose` dropped; `drain`
dropped; the hard-exit rule dropped; the crash code back to 130; images pulled on full native; the
`image_present` check dropped; no-answer treated as missing; `stdout=2` removed; `_RestartRequired`
no longer skipping; T1's exit 1 made 0; unconfirmed refs not counted; a failed pull on
native-degraded made 1; the `(detail)` dropped from the ready line; `why_not_native` dropped; the
snap path check dropped; the cert type check dropped; the logger line dropped; `backend=container`
still downloading; `backend=native` still pulling; a cancelled pull not killing.

### 7.1 The live check on a real stock Ubuntu (T6)

After the gate, with `m4-ubuntu` booted (Victor: one `Start-VM`; the rest is over the kit's SSH
key). It still has its AppArmor default, and Docker from the apt repo. A wheel from the working
tree goes into a throwaway `uv tool` dir, and:

- `setup` as is: no tree download, `ready …: isolation container`, and the `native isn't used` line
  naming Ubuntu's restriction;
- `VESTIBULE_RUNTIME=podman setup`: `Blocked:` with the same reason and a runtime fix, no `setup`
  in the message (criterion 4's text);
- the literal `userns_problem()` result, for §10.

Recorded in §12 of this plan.

## 8. Docs

Definition of done, kept to what's now false. Step 4 rewrites the README.
- `README.md`, One-time setup: `setup` runs on every platform; the comments around the `docker
  pull` lines (T5); "never pulls images itself".
- `docs/GETTING_STARTED.md`: lines 23–24, and the `setup` section (it also does the readiness check
  and the images).
- `CLAUDE.md`: one line for `setup_cli.py` in the repo tree.
- `M4-adoption.md`: a dated amendment for T1 and T2.

## 9. Size

Contract: src ~160, tests ~180. Expect src about 230 (`setup_cli.py` ~160, `select.py` ~45,
`container.py` ~30, `registry.py` ~5, `rootfs.py` and `server.py` net about −10) and tests about
550. Step 1 came in over its estimates too. Test 1's table carries most of it, and its rows are
the contract's cases.

## 10. What this step doesn't settle

- **The snap and certificate hints weren't run live.** Tests 14 and 15 replay the failures step 0
  recorded. Step 5's fresh Debian can reach the second.
- **Snap Docker still downloads the two images before it fails.** The real probe is the only
  evidence that a runtime can't work, and a later snap could fix this. A path check before the pull
  would refuse a snap that works. It costs one download, on a path the README doesn't recommend.
- **A snap daemon behind a non-snap CLI** isn't detected. The hint looks at the CLI's path.
- **The readiness check can't see a GUI client's environment.** The printed inputs are P5's answer.

## 11. Review (`skeptic`, then `verifier`, 2026-11-08)

`skeptic` read v1 with no context from this session. `verifier` checked the claims it made about
existing code and logs. Its one partial: `Selection.detail` isn't the "only" place a degraded
reason appears, since each real run also computes its own. It is the only place at selection time,
so finding 3 stands.

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | med | Stock Ubuntu refuses at the `setgroups` write, and no test exercises the probe's writes | Taken: test 10a replays that failure against the real probe source, two more mutants, and §7.1's live run (T6) |
| 2 | med | A runtime that stops answering during the image step leaves `failed` empty, so `ready` stands with node's image missing | Taken: both refs count as unconfirmed, and T1 applies |
| 3 | med | The report template omits the degraded reason, and T3 hides the only other place it shows | Taken: `isolation X (detail)` in §2.5, asserted in test 1, with a mutant |
| 4 | low | A daemon that doesn't answer `image inspect` then gets an unbounded pull | Taken: `image_present` has three states, and no answer means no pull |
| 5 | low | Snap Docker downloads images it can't run | **Declined.** The real probe is the only evidence a runtime can't work. A path check before the pull would refuse a later snap that does work, and the cost is one download on a path the README doesn't recommend. Stated in §10 |
| 6 | low | The hard-exit default reports any crash as a Ctrl+C | Taken: default 1, 130 only on `CancelledError`, and an unexpected exception is printed |
| 7 | low | A root run still downloads 123 MB nobody can use | Taken: `root_refusal()` in the native step |
| 8 | low | `main` leaves the logger at ERROR for `test_shutdown.py`; one test can't be ported as described | Taken: an autouse fixture, and test 2 replaces it |
| 9 | low | The size estimates are low | Taken: src about 230, tests about 550 |

## 12. As built (2026-11-09)

**Built as §2 says, with these changes from the diff review.** `critic` found 6 low issues and
nothing high or medium. `verifier` confirmed all 6, most with a repro, and all are fixed:
- **The runtime line named the first binary found,** not the runtime selection used. It now names
  the winner's.
- **No test covered the `uid_map` and `gid_map` writes.** Test 10a now runs for all three.
- **Test 18 couldn't see a leftover container,** and on Linux its verdict is `native`. New:
  `test_the_readiness_check_leaves_no_container_or_run_dir`, real Docker on both platforms, by the
  check's own owner label.
- **Test 18 could download the trees on a host without them.** It now skips there.
- **A Ctrl+C that ended in the hard exit printed nothing.** It says "interrupted" first.
- **Image refs and other text reached the terminal raw,** a registry's error text among them. All
  of `setup`'s lines now go through one `_say` in `rootfs.py`, which uses `audit_cli._clean`. It also
  stops `print(file=None)` from reaching stdout after `2>&-`.

These fixes touched only the findings' lines, so `critic` didn't run again.

**Red first:** on the old code, tests 12, 14 and 15 failed on the missing behavior: the "run
`setup`" message, no snap sentence, no `ca-certificates` line.

**Mutants:** 53 of 53 caught on their named tests after a green control, on Windows or in WSL as
each needs. Two notes:
- "Ctrl+C not handled" ended pytest with its own exit 2, because the `KeyboardInterrupt` escaped.
  That is the test catching it.
- A mutant dropping both the drain and the close hung the test on Windows, which is the loop-close
  hang P5's rule exists for. A hang proves nothing cleanly, so two others replaced it: one leaves a
  real run dir and one a real container.

**Gate** (the final tree, before this docs pass; `tests/test_docs.py` re-run after it):
- check_env, ruff and mypy clean on both platforms.
- Linux (376 s of 540): 3.14 full suite 935 passed, 0 skipped; 3.11 Docker-free 885; stdio 7.
- Windows: 747 passed, plus 168 POSIX/Linux-only skips.

**Live check on `m4-ubuntu` (T6, §7.1):** Ubuntu 24.04.5, kernel 7.0.0-42, the AppArmor restriction
on, Docker 29.9.3 from the apt repo, from a wheel of this tree. The log is
`C:\Users\victo\m4spike\logs\vm-step2-ubuntu.txt`.
- `userns_problem()` returned the real `[Errno 13] Permission denied: '/proc/self/setgroups'` and
  the Ubuntu hint.
- First `setup`, with both images removed beforehand: no trees, both images pulled, `ready …:
  isolation container`, the `native isn't used` line, exit 0 in 7.4 s, 0 bytes on stdout.
- Second `setup`: no pull, 0.9 s.
- The first real tool call: `isolation: container`, as `setup` said.
- `VESTIBULE_RUNTIME=podman`: `Blocked:` with the AppArmor reason and the runtime fix, exit 1, and
  no `vestibule-mcp setup` in it (criterion 4's text).
- No container and no run dir left.

**Size:** src +337 lines against about 230 (`setup_cli.py` is 196), tests about
701 against about 550. Both are over: the review added code and tests, the comments on the safety
rules take room, and lines that moved (the root check, the hint split) count as added. The three-line
`_other_live_tasks` is a copy of `server.py`'s: `setup_cli` would otherwise import the whole server.
