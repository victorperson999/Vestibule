"""`vestibule-mcp audit [N]`: the last N calls in the audit log, one line each (M3 A10;
docs/plans/M3/M3-step3-audit-command.md).

A terminal command, never the MCP server. It prints to stdout, so the server imports this
module only when the command runs. A record holds code an agent sent, and a hand-edited one can
hold anything in any field (A12). So every value from a record or the environment goes through
`_clean` before it's printed, only strings and numbers are printed at all, and each field is cut
to a cap.
"""
from __future__ import annotations

import codecs
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vestibule import audit, config

_DEFAULT_N = 20
_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+\.jsonl")  # the writer's (audit.AuditLog)
_N = re.compile(r"[0-9]{1,9}")  # ASCII digits only: `²` passes isdigit()
_ESCAPED = ("Cc", "Cf", "Cs", "Zl", "Zp")  # controls, format, surrogates, separators (R1)
_SHORT, _CODE, _REASON = 40, 60, 100  # the caps on a printed field
_KINDS = ("blocked", "start", "end")
_USAGE = "usage: vestibule-mcp audit [N]  (N: how many calls to show, default 20)"
_HINT = "no records. If your MCP config sets VESTIBULE_AUDIT_DIR, set the same value here."


def main(args: list[str]) -> int:
    """0 when everything was read, 1 when the dir setting, the dir or a file couldn't be used,
    2 for bad arguments."""
    _ascii_unless_utf8(sys.stdout)
    _ascii_unless_utf8(sys.stderr)
    if len(args) > 1 or (args and not (_N.fullmatch(args[0]) and int(args[0]) > 0)):
        _err(_USAGE)
        return 2
    n = int(args[0]) if args else _DEFAULT_N
    env = os.environ  # its own reads: server.LIMITS falls back to the defaults on a bad config
    raw = env.get("VESTIBULE_AUDIT_DIR")
    limits = config.Limits(
        audit_dir=raw or config.Limits.audit_dir,
        workspace_dir=env.get("VESTIBULE_WORKSPACE") or config.Limits.workspace_dir,
        rootfs_dir=env.get("VESTIBULE_ROOTFS_DIR") or config.Limits.rootfs_dir)
    directory, problem = config.audit_dir(limits)
    if directory is None:  # an explicit value gets the server's own text; the default, W4's
        _err(f"vestibule audit: {_clean(problem) if raw and problem else audit._NO_HOME}")
        return 1
    overlap = config._audit_problem(limits, explicit=True)  # A8, against this shell's settings
    if overlap:  # a warning, not a refusal: the MCP config may move the workspace elsewhere
        _err(f"vestibule audit: warning: {_clean(overlap)}. The server refuses that setting, "
             "so the records there may not be its own.")
    try:
        status = _show(directory, n)
        if sys.stdout is not None:  # None after `>&-`: print() then writes nothing
            sys.stdout.flush()
        return status
    except BrokenPipeError:  # POSIX: the reader quit early (`| head`); the Python docs' recipe
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _ascii_unless_utf8(stream: object) -> None:
    """A UTF-8 stream keeps its encoding; any other becomes ASCII, and what it can't encode
    becomes an escape. A legacy 8-bit charset maps printable characters onto 0x80–0x9F (in
    KOI8-R, `⌡` is 0x9B; in cp1252, `›` is), which a terminal can act on as C1 controls (R4)."""
    if isinstance(stream, io.TextIOWrapper):  # not None, and a stream that can be reconfigured
        utf8 = codecs.lookup(stream.encoding).name == "utf-8"
        stream.reconfigure(encoding=None if utf8 else "ascii", errors="backslashreplace")


def _show(directory: Path, n: int) -> int:
    shown = _clean(str(directory))
    try:
        # A symlink isn't followed: the writer never makes one, and a looping one would
        # stop the whole listing.
        names = sorted(e.name for e in os.scandir(directory)
                       if _NAME.fullmatch(e.name) and e.is_file(follow_symlinks=False))
    except FileNotFoundError:
        names = []
    except OSError as e:
        _err(f"vestibule audit: {_clean(str(e))}")
        return 1
    print(f"audit records in {shown}")
    calls: list[_Call] = []
    tally = _Tally()
    for name in names:
        try:
            _read(directory / name, name, calls, tally)
        except OSError as e:  # deleted since the listing, no permission: show the rest
            tally.failed += 1
            _err(f"vestibule audit: {_clean(str(e))}")
    calls.sort(key=lambda c: c.key)
    for call in calls[-n:]:
        print(_row(call))
    extras = []
    if tally.skipped:
        extras.append(f"{_count(tally.skipped, 'unreadable line')} skipped")
    if tally.failed:
        extras.append(f"{_count(tally.failed, 'file')} couldn't be read")
    if calls:
        total = f"{min(n, len(calls))} of {_count(len(calls), 'call')}"
        print("; ".join([f"{total}, from {_count(len(names), 'file')}", *extras]))
    else:
        print(_HINT)
        if extras:
            print("; ".join(extras))
    return 1 if tally.failed else 0


# --- reading -------------------------------------------------------------------------------

@dataclass
class _Tally:
    skipped: int = 0
    failed: int = 0


@dataclass
class _Call:
    key: tuple[str, str, int]  # its first record's ts, file name and line number
    start: dict[str, str] | None = None  # each record cut down to its printed fields
    end: dict[str, str] | None = None
    blocked: dict[str, str] | None = None


def _read(path: Path, name: str, calls: list[_Call], tally: _Tally) -> None:
    """One file's calls. One process writes both records of a call, so pairing is per file."""
    open_calls: dict[str, _Call] = {}
    with open(path, "rb") as f:
        for number, line in enumerate(f):
            rec = _record(line)
            if rec is None:
                tally.skipped += 1
                continue
            key = (rec["ts"], name, number)
            fields = _cut_down(rec)
            if rec["kind"] == "start":
                call = _Call(key, start=fields)
                open_calls[rec["id"]] = call
                calls.append(call)
            elif rec["kind"] == "end" and rec["id"] in open_calls:
                open_calls.pop(rec["id"]).end = fields
            elif rec["kind"] == "end":  # its start was torn, or never written
                calls.append(_Call(key, end=fields))
            else:
                calls.append(_Call(key, blocked=fields))


def _record(line: bytes) -> dict[str, Any] | None:
    """A usable record, or None for a line to skip and count."""
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):  # torn, not JSON, nested past the parser's limit
        return None
    if (isinstance(rec, dict) and type(rec.get("v")) is int and rec["v"] == 1
            and rec.get("kind") in _KINDS
            and isinstance(rec.get("id"), str) and isinstance(rec.get("ts"), str)):
        return rec
    return None


def _cut_down(rec: dict[str, Any]) -> dict[str, str]:
    """The record's printed fields, cleaned and cut; the code is dropped after its snippet.
    Can't raise: `str()` only ever sees a string or a number."""
    fields = {"ts": _value(rec, "ts")}
    if rec["kind"] == "start":
        fields["language"] = _value(rec, "language")
        fields["code"] = _snippet(rec.get("code"), _CODE, rec.get("code_truncated") is True)
    elif rec["kind"] == "blocked":
        fields.update(language=_value(rec, "language"), outcome=_value(rec, "outcome"),
                      reason=_snippet(rec.get("reason"), _REASON))
    else:
        usage = rec.get("usage")
        if isinstance(usage, dict):
            cpu, mem = _value(usage, "cpu_ms", "ms"), _value(usage, "mem_peak_mb", "MiB")
        else:
            cpu = mem = "-" if usage is None else "?"
        fields.update(outcome=_value(rec, "outcome"), isolation=_value(rec, "isolation"),
                      exit=_value(rec, "exit_code"), wall=_value(rec, "wall_ms", "ms"),
                      cpu=cpu, mem=mem, denied=_denied(rec.get("denied_syscalls")),
                      reason=_snippet(rec.get("reason"), _REASON))
    return fields


def _value(rec: dict[str, Any], key: str, unit: str = "") -> str:
    """A short field: `-` when missing or null, `?` when it isn't a string or a number."""
    v = rec.get(key)
    if v is None:
        return "-"
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return "?"
    text = _cut(_clean(str(v)[:_SHORT + 1]), _SHORT)  # cleaning only lengthens: same result
    return text if isinstance(v, str) else text + unit


def _denied(v: object) -> str:
    """The denied syscalls' names (R5), or "" when there are none."""
    if v is None:
        return ""
    if not isinstance(v, list):
        return "?"
    return _cut(_clean(",".join(name for name in v if isinstance(name, str))[:_CODE + 1]), _CODE)


def _snippet(v: object, cap: int, truncated: bool = False) -> str:
    """The first line that isn't blank, cleaned and cut (R2). `...` when it was cut, when more
    lines follow, or when the record kept only the start of the code."""
    if v is None:
        return "-"
    if not isinstance(v, str):
        return "?"
    lines = v.splitlines()  # every line break, \r and U+2028 included
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        text, more = "(blank)", False
    else:
        # Every record is read, so only what can be shown is cleaned: cleaning only lengthens
        # text, so the first cap + 1 characters decide the result.
        text = _clean(lines[first].strip()[:cap + 1])
        more = any(line.strip() for line in lines[first + 1:])
    if len(text) > cap:
        text, more = text[:cap], True
    return text + "..." if more or truncated else text


# --- printing ------------------------------------------------------------------------------

def _row(call: _Call) -> str:
    start, end, blocked = call.start, call.end, call.blocked
    first = start or blocked or end
    assert first is not None
    language = (start or blocked or {}).get("language", "-")
    if blocked is not None:
        outcome = blocked["outcome"]
        rest = ["(cancelled before a backend was chosen)" if outcome == "cancelled"
                else blocked["reason"]]
    elif end is not None:
        outcome = end["outcome"]
        if outcome == "cancelled":
            rest = [f"wall={end['wall']}"]
        elif outcome in ("refused", "failed"):  # no RunResult: the reason, and the code (R3)
            rest = [f"wall={end['wall']}", end["reason"]]
        else:
            rest = [f"isolation={end['isolation']}", f"exit={end['exit']}",
                    f"wall={end['wall']}"]
            rest += [f"{k}={end[f]}" for k, f in (("cpu", "cpu"), ("mem_peak", "mem"))
                     if end[f] != "-"]
            if end["denied"]:
                rest.append(f"denied={end['denied']}")
        if start is None:
            rest.append("(no start record found)")
    else:
        outcome = "no end"
        rest = ["(no end record: still running, the server stopped, or the record was lost)"]
    row = f"{first['ts']}  {language:<6}  {outcome:<9}  " + " ".join(rest)
    return f"{row}  {start['code']}" if start is not None else row


def _clean(text: str) -> str:
    """Every control, format character, surrogate and line or paragraph separator as an
    escape (R1), so nothing printed can drive the terminal or hide text. Backslashes aren't
    doubled: code is full of `\\n` inside strings."""
    return "".join(_escape(c) if unicodedata.category(c) in _ESCAPED else c for c in text)


def _escape(c: str) -> str:
    n = ord(c)
    return f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000 else f"\\U{n:08x}"


def _cut(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "..."


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _err(msg: str) -> None:
    if sys.stderr is not None:  # print(file=None) would write to stdout instead
        print(msg, file=sys.stderr)
