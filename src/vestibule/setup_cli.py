"""`vestibule-mcp setup`: get this machine ready, and say what a client will get (M4 P4 and P5,
docs/plans/M4/M4-step2-setup.md).

It downloads only what this machine can use, then runs the server's own selection once and prints
the answer, so there is no second wording to keep honest. Only a human runs it: nothing here is
reachable from a tool call (D2). Everything goes to stderr, the runtime's own output included
(golden rule 1).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

from vestibule import rootfs
from vestibule.backends.base import RunRefusedError
from vestibule.backends.container import ContainerBackend
from vestibule.backends.select import (
    _PINNED_REF,
    _RUNTIME_ORDER,
    BackendSelector,
    Selection,
    _RestartRequired,
    root_refusal,
    userns_problem,
)
from vestibule.config import ConfigError, Limits

_DRAIN_S = 10  # the winning probe's cleanup, as for a rejected candidate's (select.py)
_say = rootfs._say


def main() -> int:
    """0 when the final check says this machine is ready, 1 otherwise, 130 on Ctrl+C (P4)."""
    # Selection's lines are for an operator's log. Here each check would print them and the
    # report below would say it all again (T3).
    logging.getLogger("vestibule").setLevel(logging.ERROR)
    try:
        limits = Limits.from_env()  # not server.LIMITS: that falls back to the defaults
    except ConfigError as e:
        _say("vestibule setup: invalid settings, nothing was changed:")
        for problem in e.problems:
            _say(f"  - {problem}")
        return 1
    try:
        _native_step(limits)
        return asyncio.run(_ready_steps(limits))
    except KeyboardInterrupt:
        _say("vestibule setup: interrupted")
        return 130


def _native_step(limits: Limits) -> None:
    """P4.1: the trees, on a machine that can use them."""
    if sys.platform != "linux":
        _say("vestibule setup: native isolation is Linux-only, so this machine uses a "
             "container runtime")
        return
    if limits.backend not in ("auto", "native"):
        _say(f"vestibule setup: VESTIBULE_BACKEND={limits.backend!r}: skipping the native trees")
        return
    refused = root_refusal() or userns_problem()
    if refused:
        _say(f"vestibule setup: not downloading the native trees (about 123 MB): {refused}")
        return
    rootfs.install(limits)  # it prints its own failures; the final check decides the exit code


async def _ready_steps(limits: Limits) -> int:
    code = 1
    try:
        code = await _steps(limits)
    except asyncio.CancelledError:  # Ctrl+C
        code = 130
        raise
    except Exception as e:  # the hard exit below would otherwise hide it
        _say(f"vestibule setup: failed: {e!r}")
    finally:
        # S7-D1's rule (server._shutdown): asyncio.run's cancel-all can catch a task
        # mid-subprocess-spawn and hang the loop's close forever on Windows.
        if _other_live_tasks():
            if code == 130:  # main's own message never runs after this
                _say("vestibule setup: interrupted")
            os._exit(code)
    return code


def _other_live_tasks() -> list[asyncio.Task]:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


async def _steps(limits: Limits) -> int:
    _say("vestibule setup: checking what this machine can run")
    result = await _check(limits)
    unconfirmed: list[str] = []
    if _wants_images(limits, result):
        pulled, unconfirmed = await _image_step(limits)
        if pulled:
            _say("vestibule setup: checking again")
            result = await _check(limits)
    return _report(limits, result, unconfirmed)


async def _check(limits: Limits) -> Selection | RunRefusedError:
    """P5: the server's real selection, from a fresh selector (each caches its verdict)."""
    selector = BackendSelector()
    try:
        selection = await selector.get(limits)
        await selection.warden.drain(_DRAIN_S)  # the probe's cleanup, run dir included
        return selection
    except RunRefusedError as e:
        return e
    except Exception as e:  # a workspace that can't be made, say: a line, not a traceback
        return RunRefusedError(f"the readiness check failed: {e}")
    finally:
        await selector.aclose()


def _wants_images(limits: Limits, result: Selection | RunRefusedError) -> bool:
    if limits.backend not in ("auto", "container"):
        return False
    if isinstance(result, _RestartRequired):  # a setting, root, or no runtime: a pull can't help
        return False
    return isinstance(result, RunRefusedError) or result.verdict != "native"


async def _image_step(limits: Limits) -> tuple[bool, list[str]]:
    """P4.3: whether anything was pulled, and the images not confirmed present afterwards."""
    refs = [limits.image_python, limits.image_node]
    try:
        runtime = await BackendSelector()._resolve_runtime(limits)
    except _RestartRequired:  # none installed: the check's own message says so
        return False, []
    except RunRefusedError as e:  # the daemon is stopped, or not in the docker group
        _say(f"vestibule setup: can't get the sandbox images yet: {e}")
        return False, refs
    backend = ContainerBackend(runtime)
    pulled = False
    unconfirmed: list[str] = []
    for ref in refs:
        present = await backend.image_present(ref)
        if present:
            continue
        if present is None:  # no answer: a pull would hang the same way (T4)
            _say(f"vestibule setup: {runtime} didn't answer about {ref}, so it wasn't pulled")
            unconfirmed.append(ref)
            continue
        _say(f"vestibule setup: pulling {ref} with {runtime}")
        if await backend.pull_image(ref) == 0:
            pulled = True
        else:
            _say(f"vestibule setup: could not pull {ref}; the reason is above")
            unconfirmed.append(ref)
    return pulled, unconfirmed


def _report(limits: Limits, result: Selection | RunRefusedError, unconfirmed: list[str]) -> int:
    """P5's lines and the exit code."""
    code = 0
    if isinstance(result, RunRefusedError):
        _say(f"Blocked: {result}")
        code = 1
    else:
        isolation = result.verdict + (f" ({result.detail})" if result.detail else "")
        if unconfirmed and result.verdict.startswith("container"):  # T1: its probe uses one image
            _say(f"not ready: isolation {isolation}, but these images are missing or "
                 f"unconfirmed: {', '.join(unconfirmed)}")
            code = 1
        else:
            _say(f"ready in this terminal's environment: isolation {isolation}")
        if result.why_not_native:
            _say(f"  native isn't used: {result.why_not_native}")
    _say_inputs(limits, result)
    return code


def _say_inputs(limits: Limits, result: Selection | RunRefusedError) -> None:
    """P5: what can differ for a client that wasn't started from this terminal."""
    _say("  a client started another way can differ in these:")
    order = _RUNTIME_ORDER.get(limits.runtime, ())
    if isinstance(result, Selection) and isinstance(result.warden, ContainerBackend):
        order = (result.warden.runtime,)  # the one selection used, not just the first installed
    runtime = next(filter(None, map(shutil.which, order)), None)
    _say(f"  container runtime on this PATH: {runtime or 'none'}")
    settings = sorted(k for k, v in os.environ.items()
                      if k.startswith("VESTIBULE_") and v.strip())
    for name in settings:
        value = os.environ[name]
        # S6-D2: an unpinned image is never silent. Only an override can be unpinned.
        unpinned = name.startswith("VESTIBULE_IMAGE_") and not _PINNED_REF.search(value)
        _say(f"  {name}={value!r}" + (" (not digest-pinned)" if unpinned else ""))
    if not settings:
        _say("  no VESTIBULE_* settings")
