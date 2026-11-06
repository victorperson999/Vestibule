"""`vestibule-mcp audit [N]` (M3 step 3, docs/plans/M3/M3-step3-audit-command.md §5).

Most tests write JSONL by hand into `tmp_path`, so each one controls every field. One reads a
real `AuditLog`'s file while the writer still holds it open, and two run the console script.
Checks on characters are made on decoded text, never on bytes: `Û` is `C3 9B` in UTF-8.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
import unicodedata

import pytest
from homeutil import NoHome, RelativeHome
from stdioutil import SERVER_EXE

from vestibule import audit, audit_cli, config
from vestibule.backends.base import RunResult

A = "20261103T210000Z-100.jsonl"  # sorts before B
B = "20261103T210500Z-200.jsonl"
HINT = "no records. If your MCP config sets VESTIBULE_AUDIT_DIR, set the same value here."
RLO = chr(0x202E)  # written with chr(): no raw bidi control in this file


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # The dev shell's own settings must not leak into these tests.
    for name in list(os.environ):
        if name.startswith("VESTIBULE_"):
            monkeypatch.delenv(name)


def _ts(n: int) -> str:
    return f"2026-11-03T21:{n // 60:02d}:{n % 60:02d}.000Z"


def _start(i: str, n: int, code: str = "print(1)", language: str = "python", **kw) -> dict:
    return {"v": 1, "kind": "start", "id": i, "ts": _ts(n), "language": language,
            "code": code, "code_truncated": False, **kw}


def _end(i: str, n: int, outcome: str = "ran", **kw) -> dict:
    rec = {"v": 1, "kind": "end", "id": i, "ts": _ts(n), "outcome": outcome,
           "isolation": "native", "exit_code": 0, "wall_ms": 84,
           "usage": {"cpu_ms": 31, "mem_peak_mb": 11, "pids_peak": 2}, "denied_syscalls": []}
    rec.update(kw)
    return rec


def _blocked(n: int, outcome: str = "refused", reason: str | None = "code is empty.",
             language: str = "python") -> dict:
    rec = {"v": 1, "kind": "blocked", "id": f"b{n}", "ts": _ts(n), "language": language,
           "outcome": outcome}
    if reason is not None:
        rec["reason"] = reason
    return rec


def _write(directory, name: str, *lines) -> None:
    """Each line a record (a dict) or raw bytes, each ended with `\\n`."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / name, "ab") as f:
        for line in lines:
            f.write((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n")


def _run(monkeypatch, capsys, directory, *args: str) -> tuple[int, str, str]:
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", str(directory))
    status = audit_cli.main(list(args))
    out, err = capsys.readouterr()
    return status, out, err


def _rows(out: str) -> list[str]:
    """The call rows: everything between the header and the summary."""
    return out.splitlines()[1:-1]


# --- 5.1 pure --------------------------------------------------------------------------------

@pytest.mark.parametrize("char, escaped", [
    ("\x00", "\\x00"), ("\t", "\\x09"), ("\n", "\\x0a"), ("\r", "\\x0d"), ("\x1b", "\\x1b"),
    ("\x7f", "\\x7f"), ("\x85", "\\x85"), ("\x9b", "\\x9b"), ("\x9d", "\\x9d"),
    *[(chr(c), f"\\u{c:04x}") for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A),
                                        0x200E, 0x200B, 0x061C, 0xFEFF, 0x2028, 0x2029)],
    (chr(0xE0041), "\\U000e0041"), (chr(0xD800), "\\ud800"),
])
def test_clean_escapes_every_control_and_format_character(char, escaped):
    assert audit_cli._clean(f"a{char}b") == f"a{escaped}b"


@pytest.mark.parametrize("text", ["héllo wörld 日本 😀", "a\\b", "\\x1b typed"])
def test_clean_keeps_text_and_backslashes(text):
    assert audit_cli._clean(text) == text


@pytest.mark.parametrize("code, truncated, shown", [
    ("\n\nfoo\nbar", False, "foo..."),
    ("a\rb", False, "a..."),
    ("a" + chr(0x2028) + "b", False, "a..."),
    ("a\x1cb", False, "a..."),
    ("print(1)\n", False, "print(1)"),
    ("print(1)\n\n  \n", False, "print(1)"),  # trailing blank lines aren't "more"
    ("x" * 60, False, "x" * 60),
    ("x" * 61, False, "x" * 60 + "..."),
    ("\x1b" * 30, False, "\\x1b" * 15 + "..."),  # cleaned first, then cut
    ("    import os", False, "import os"),
    (" \n\t\n", False, "(blank)"),
    (" " * 4096, True, "(blank)..."),  # 5,000 spaces, then code: the record kept 4 KiB
    ("print(1)", True, "print(1)..."),
])
def test_snippet_is_the_first_line_that_isnt_blank(code, truncated, shown):
    assert audit_cli._snippet(code, 60, truncated) == shown


def test_no_argument_shows_the_last_20(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, *[r for i in range(25)
                          for r in (_start(f"c{i}", 2 * i, f"c{i}"), _end(f"c{i}", 2 * i + 1))])
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert (status, err) == (0, "")
    rows = _rows(out)
    assert len(rows) == 20 and rows[0].endswith("  c5") and rows[-1].endswith("  c24")
    assert out.splitlines()[-1] == "20 of 25 calls, from 1 file"


def test_n_shows_the_last_n(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, *[r for i in range(25)
                          for r in (_start(f"c{i}", 2 * i, f"c{i}"), _end(f"c{i}", 2 * i + 1))])
    status, out, _ = _run(monkeypatch, capsys, tmp_path, "3")
    assert status == 0
    assert [r.rsplit("  ", 1)[1] for r in _rows(out)] == ["c22", "c23", "c24"]


@pytest.mark.parametrize("args", [["0"], ["-1"], ["abc"], ["²"], ["1", "2"], ["1234567890"],
                                  [""], ["020x"]])
def test_bad_arguments_print_the_usage(tmp_path, monkeypatch, capsys, args):
    status, out, err = _run(monkeypatch, capsys, tmp_path, *args)
    assert (status, out) == (2, "")
    assert err == "usage: vestibule-mcp audit [N]  (N: how many calls to show, default 20)\n"


# --- 5.2 over files --------------------------------------------------------------------------

def test_two_processes_files_merge_in_time_order(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _start("a1", 1, "a1"), _end("a1", 2, exit_code=1),
           _start("same", 5, "a2"), _end("same", 6, exit_code=2),
           _start("lost", 9, "a-lost"),  # its end is in the other process's file
           _start("tie", 10, "a-tie"), _end("tie", 11, exit_code=5))
    _write(tmp_path, B, _start("same", 3, "b1"), _end("same", 4, exit_code=3),
           _start("b2", 7, "b2"), _end("b2", 8, exit_code=4),
           _end("lost", 12, exit_code=6),
           _start("tie", 10, "b-tie"), _end("tie", 11, exit_code=7))
    status, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert status == 0
    rows = _rows(out)
    assert [r.split("  ")[-1] for r in rows[:7]] == ["a1", "b1", "a2", "b2", "a-lost", "a-tie",
                                                    "b-tie"]
    assert ["exit=1", "exit=3", "exit=2", "exit=4"] == [r.split()[4] for r in rows[:4]]
    assert "no end" in rows[4]  # pairing is per file: B's `end` isn't a-lost's
    assert "exit=6" in rows[7] and rows[7].endswith("(no start record found)")
    assert out.splitlines()[-1] == "8 of 8 calls, from 2 files"
    _, out, _ = _run(monkeypatch, capsys, tmp_path, "3")
    assert [r.split()[4] for r in _rows(out)] == ["exit=5", "exit=7", "exit=6"]


def test_every_row_kind_exactly(tmp_path, monkeypatch, capsys):
    no_usage = {"cpu_ms": None, "mem_peak_mb": None, "pids_peak": None}
    _write(tmp_path, A,
           _start("1", 1), _end("1", 2, denied_syscalls=["mount"]),
           _start("2", 3, "while true; do :; done", "bash"),
           _end("2", 4, "timed_out", isolation="container", exit_code=-1, wall_ms=10204,
                usage=no_usage),
           _start("3", 5, "console.log(1)", "node"),
           {"v": 1, "kind": "end", "id": "3", "ts": _ts(6), "outcome": "refused",
            "wall_ms": 5003, "reason": "too many concurrent runs (max 4); retry shortly"},
           _start("4", 7, "import time\ntime.sleep(99)"),
           {"v": 1, "kind": "end", "id": "4", "ts": _ts(8), "outcome": "failed",
            "wall_ms": 40012, "reason": "Execution failed: sandbox did not return in time."},
           _blocked(9),
           _blocked(10, "failed", "Internal error: boom", "node"),
           _blocked(11, "cancelled", None),
           _start("8", 12, "input()"),
           {"v": 1, "kind": "end", "id": "8", "ts": _ts(13), "outcome": "cancelled",
            "wall_ms": 1520},
           _start("9", 14, "import os\nos.getcwd()"),
           _end("10", 15, wall_ms=90, usage={"cpu_ms": 5, "mem_peak_mb": 9, "pids_peak": 1}),
           _start("11", 16, "print(2)"),
           _end("11", 17, "not_run", isolation="none", exit_code=127, wall_ms=3, usage=no_usage))
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert (status, err) == (0, "")
    assert out == "\n".join([
        f"audit records in {tmp_path}",
        "2026-11-03T21:00:01.000Z  python  ran        isolation=native exit=0 wall=84ms cpu=31ms "
        "mem_peak=11MiB denied=mount  print(1)",
        "2026-11-03T21:00:03.000Z  bash    timed_out  isolation=container exit=-1 wall=10204ms  "
        "while true; do :; done",
        "2026-11-03T21:00:05.000Z  node    refused    wall=5003ms too many concurrent runs "
        "(max 4); retry shortly  console.log(1)",
        "2026-11-03T21:00:07.000Z  python  failed     wall=40012ms Execution failed: sandbox "
        "did not return in time.  import time...",
        "2026-11-03T21:00:09.000Z  python  refused    code is empty.",
        "2026-11-03T21:00:10.000Z  node    failed     Internal error: boom",
        "2026-11-03T21:00:11.000Z  python  cancelled  (cancelled before a backend was chosen)",
        "2026-11-03T21:00:12.000Z  python  cancelled  wall=1520ms  input()",
        "2026-11-03T21:00:14.000Z  python  no end     (no end record: still running, the server "
        "stopped, or the record was lost)  import os...",
        "2026-11-03T21:00:15.000Z  -       ran        isolation=native exit=0 wall=90ms cpu=5ms "
        "mem_peak=9MiB (no start record found)",
        "2026-11-03T21:00:16.000Z  python  not_run    isolation=none exit=127 wall=3ms  print(2)",
        "11 of 11 calls, from 1 file",
        "",
    ])


UNREADABLE = [
    b'{"v":1,"kind":"start","id":"t","ts":"2026-1',  # torn
    b"",  # blank
    b"[1, 2]",
    json.dumps({"v": 1, "kind": "start", "id": "x"}).encode(),  # no ts
    json.dumps({"v": 1, "kind": "start", "id": 5, "ts": _ts(1)}).encode(),
    json.dumps({"v": 1, "kind": "other", "id": "x", "ts": _ts(1)}).encode(),
    json.dumps({"v": 2, "kind": "start", "id": "x", "ts": _ts(1)}).encode(),
    json.dumps({"v": True, "kind": "start", "id": "x", "ts": _ts(1)}).encode(),
    json.dumps({"v": 1.0, "kind": "start", "id": "x", "ts": _ts(1)}).encode(),
    b"[" * 100_000,
]


def test_unreadable_lines_are_skipped_and_counted(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _start("1", 1), _end("1", 2), *UNREADABLE, _blocked(3))
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert (status, err) == (0, "")
    assert len(_rows(out)) == 2
    assert out.splitlines()[-1] == "2 of 2 calls, from 1 file; 10 unreadable lines skipped"


def test_a_recursion_error_from_the_parser_is_a_skipped_line(tmp_path, monkeypatch, capsys):
    def loads(line):  # the depth limit differs between interpreters, so raise it on purpose
        if b"deep" in line:
            raise RecursionError("maximum recursion depth exceeded")
        return json.loads(line)

    monkeypatch.setattr(audit_cli, "json", types.SimpleNamespace(loads=loads))
    _write(tmp_path, A, b'["deep"]', _blocked(1))
    status, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert status == 0
    assert out.splitlines()[-1] == "1 of 1 call, from 1 file; 1 unreadable line skipped"


def test_wrong_field_types_show_a_question_mark(tmp_path, monkeypatch, capsys):
    nested = json.loads("[" * 500 + "]" * 500)
    _write(tmp_path, A,
           _start("1", 1), _end("1", 2, usage=5, exit_code={"a": 1}, denied_syscalls="mount",
                                wall_ms=True, isolation="i" * 30_000),
           _blocked(3, reason=nested),
           _start("4", 4, code=["print(1)"]))
    status, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert status == 0
    end, blocked, start = _rows(out)
    assert f"isolation={'i' * 40}... exit=? wall=? cpu=? mem_peak=? denied=?  print(1)" in end
    assert blocked.endswith("refused    ?")
    assert start.endswith("the record was lost)  ?")


def test_a_start_without_end_and_an_end_without_start(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _start("1", 1, "import os"), _end("2", 2))  # no unreadable line
    status, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert status == 0
    first, second = _rows(out)
    assert first.endswith("  no end     (no end record: still running, the server stopped, "
                          "or the record was lost)  import os")
    assert second.endswith("(no start record found)")
    assert out.splitlines()[-1] == "2 of 2 calls, from 1 file"


def _hostile() -> list[dict]:
    """Criterion 7's record, and every other printed field carrying ESC."""
    code = "\x1b[8m" + RLO + "evil()\rprint('safe')"
    return [
        _start("h", 1, code), _end("h", 2, denied_syscalls=["mount\x1b[5m"]),
        _blocked(3, reason="\x1b]52;c;ZWNobyBoaQ==\x07 refused"),
        {"v": 1, "kind": "start", "id": "e", "ts": _ts(4) + "\x1b[2J", "language": "py\x1b[31m",
         "code": "print('日本 é')"},
        _end("e", 5, outcome="ran\x1b[0m", isolation="native\x1b[1A", wall_ms="1\x1b[K"),
    ]


def test_a_hostile_record_is_printed_escaped(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, *_hostile())
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert (status, err) == (0, "")
    assert not [c for c in out.replace("\n", "")
                if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")]
    for shown in ("\\x1b[8m\\u202eevil()...", "denied=mount\\x1b[5m",
                  "\\x1b]52;c;ZWNobyBoaQ==\\x07 refused", "\\x1b[2J", "py\\x1b[31m",
                  "ran\\x1b[0m", "isolation=native\\x1b[1A", "wall=1\\x1b[K", "日本 é"):
        assert shown in out
    assert "print('safe')" not in out  # after the \r: never shown, never overwriting the row


# Printable characters that a legacy 8-bit charset puts on 0x80-0x9F, where a terminal can
# take them for C1 controls: KOI8-R's CSI, OSC and ST, and cp1252's CSI.
EIGHT_BIT = chr(0x2321) + "[2J " + chr(0xB2) + "52;c;aGk=" + chr(0xB0) + " " + chr(0x203A)


@pytest.mark.parametrize("encoding", ["cp1252", "koi8_r"])  # a Windows pipe; a KOI8-R terminal
def test_a_legacy_8bit_stdout_gets_ascii(tmp_path, monkeypatch, encoding):
    _write(tmp_path, A, *_hostile(), _start("k", 6, EIGHT_BIT))
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", str(tmp_path))
    stdout = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert audit_cli.main([]) == 0
    stdout.flush()
    data = stdout.buffer.getvalue()
    assert max(data) < 0x80  # R4: no byte a terminal could act on as an 8-bit C1 control
    out = data.decode("ascii")
    assert "print('\\u65e5\\u672c \\xe9')" in out
    assert "\\u2321[2J \\xb252;c;aGk=\\xb0 \\u203a" in out
    assert "\\x1b[8m\\u202eevil()..." in out


def test_a_legacy_8bit_stderr_gets_ascii(monkeypatch):
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="koi8_r")
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", "relative" + chr(0x2321))
    assert audit_cli.main([]) == 1
    stderr.flush()
    data = stderr.buffer.getvalue()
    assert max(data) < 0x80 and b"relative\\u2321" in data


def test_no_stdout_at_all(tmp_path, monkeypatch):  # `vestibule-mcp audit >&-` on POSIX
    _write(tmp_path, A, _blocked(1))
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    assert audit_cli.main([]) == 0


def test_long_fields_are_cut_through_main(tmp_path, monkeypatch, capsys):
    names = [f"call{i:02d}" for i in range(20)]
    _write(tmp_path, A, _start("1", 1, "x" * 80), _end("1", 2, denied_syscalls=names),
           _blocked(3, reason="r" * 150))
    _, out, _ = _run(monkeypatch, capsys, tmp_path)
    end, blocked = _rows(out)
    assert end.endswith(f" denied={','.join(names)[:60]}...  {'x' * 60}...")
    assert blocked.endswith("  refused    " + "r" * 100 + "...")


def test_a_missing_or_empty_dir_gives_the_hint(tmp_path, monkeypatch, capsys):
    for directory in (tmp_path / "missing", tmp_path):
        status, out, err = _run(monkeypatch, capsys, directory)
        assert (status, out, err) == (0, f"audit records in {directory}\n{HINT}\n", "")


def test_the_hint_keeps_the_skip_count(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, b"torn")
    _, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert out.splitlines()[1:] == [HINT, "1 unreadable line skipped"]


def test_a_file_instead_of_a_dir_is_an_error(tmp_path, monkeypatch, capsys):
    (tmp_path / "f").write_text("x")
    status, out, err = _run(monkeypatch, capsys, tmp_path / "f")
    assert (status, out) == (1, "")
    assert err.startswith("vestibule audit: ")


def test_only_audit_files_are_read(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _blocked(1))
    _write(tmp_path, "notes.jsonl", _blocked(2))
    (tmp_path / B).mkdir()
    _, out, _ = _run(monkeypatch, capsys, tmp_path)
    assert len(_rows(out)) == 1 and out.splitlines()[-1] == "1 of 1 call, from 1 file"


def test_a_file_that_cant_be_read_is_named_and_the_rest_shown(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _blocked(1))
    _write(tmp_path, B, _blocked(2))
    real = open

    def fake_open(path, *args, **kw):
        if str(path).endswith(B):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kw)

    monkeypatch.setattr(audit_cli, "open", fake_open, raising=False)
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert status == 1
    assert err.startswith("vestibule audit: ") and B in err
    assert len(_rows(out)) == 1
    assert out.splitlines()[-1] == "1 of 1 call, from 2 files; 1 file couldn't be read"


def test_the_dir_from_the_env_is_cleaned(tmp_path, monkeypatch, capsys):
    # A missing dir prints the header on POSIX; Windows refuses the name (WinError 123).
    _, out, err = _run(monkeypatch, capsys, tmp_path / "a\x1bb")
    assert "\x1b" not in out + err and "a\\x1bb" in out + err


def test_the_default_dir_follows_the_home_directory(tmp_path, monkeypatch, capsys):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    expected = f"audit records in {tmp_path / '.vestibule' / 'audit'}\n{HINT}\n"
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("VESTIBULE_AUDIT_DIR", raising=False)
        else:
            monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value)
        assert audit_cli.main([]) == 0
        assert capsys.readouterr().out == expected  # never the real dir, or server.LIMITS'


@pytest.mark.parametrize("stand_in", [NoHome, RelativeHome])
def test_a_default_without_a_usable_home_is_an_error(monkeypatch, capsys, stand_in):
    monkeypatch.setattr(config, "Path", stand_in)
    assert audit_cli.main([]) == 1
    assert capsys.readouterr() == ("", "vestibule audit: could not find the home directory; "
                                       "set VESTIBULE_AUDIT_DIR\n")


@pytest.mark.parametrize("stand_in, value, message", [
    (RelativeHome, "~/x", "VESTIBULE_AUDIT_DIR='~/x' expands to "),
    (NoHome, "~/x", "VESTIBULE_AUDIT_DIR='~/x' must be an absolute path or start with ~ (Could "
                    "not determine home directory.)"),
    (None, "relative/x", "VESTIBULE_AUDIT_DIR='relative/x' must be an absolute path or start "
                         "with ~"),
])
def test_an_explicit_value_gets_the_servers_message(monkeypatch, capsys, stand_in, value,
                                                     message):
    if stand_in is not None:
        monkeypatch.setattr(config, "Path", stand_in)
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", value)
    assert audit_cli.main([]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith(f"vestibule audit: {message}")


def test_a_home_holding_esc_is_cleaned_in_the_message(monkeypatch, capsys):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, "home\x1b]0;x\x07")  # relative: the "expands to" message
    monkeypatch.setenv("VESTIBULE_AUDIT_DIR", "~/x")
    assert audit_cli.main([]) == 1
    err = capsys.readouterr().err
    assert "\x1b" not in err and "expands to home\\x1b]0;x\\x07" in err


def test_a_dir_the_server_would_refuse_gets_a_warning(tmp_path, monkeypatch, capsys):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    _write(tmp_path / ".vestibule" / "workspace" / "audit", A, _blocked(1))  # sandbox-written
    status, out, err = _run(monkeypatch, capsys, "~/.vestibule/workspace/audit")
    assert status == 0 and len(_rows(out)) == 1
    assert err.startswith("vestibule audit: warning: the audit directory (")
    assert err.endswith("overlap; point one of them somewhere else. The server refuses that "
                        "setting, so the records there may not be its own.\n")


@pytest.mark.parametrize("setting", ["VESTIBULE_WORKSPACE", "VESTIBULE_ROOTFS_DIR"])
def test_the_shells_workspace_and_rootfs_settings_are_checked(tmp_path, monkeypatch, capsys,
                                                              setting):
    other = tmp_path / "o\x1bx"  # never made, and named so the message must be cleaned
    monkeypatch.setenv(setting, str(other))
    _, _, err = _run(monkeypatch, capsys, other / "audit")
    assert "vestibule audit: warning: the audit directory (" in err
    assert f", {setting}) overlap" in err
    assert "\x1b" not in err and "o\\x1bx" in err


def test_a_looping_symlink_is_skipped_not_followed(tmp_path, monkeypatch, capsys):
    _write(tmp_path, A, _blocked(1))
    try:
        os.symlink(tmp_path / B, tmp_path / B)  # it points at itself
    except OSError as e:  # Windows without the symlink privilege
        pytest.skip(f"symlinks can't be made here: {e}")
    status, out, err = _run(monkeypatch, capsys, tmp_path)
    assert (status, err) == (0, "") and out.splitlines()[-1] == "1 of 1 call, from 1 file"


def test_no_stderr_keeps_errors_off_stdout(monkeypatch, capsys):  # `2>&-` on POSIX
    monkeypatch.setattr(sys, "stderr", None)
    assert audit_cli.main(["0"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("where", ["scandir", "open"])
def test_an_os_error_message_is_cleaned(tmp_path, monkeypatch, capsys, where):
    _write(tmp_path, A, _blocked(1))

    def fail(*args, **kw):  # a Windows system message can hold CR LF
        raise OSError(384, "line one\r\nline two")

    monkeypatch.setattr(os if where == "scandir" else audit_cli, where, fail, raising=False)
    status, _, err = _run(monkeypatch, capsys, tmp_path)
    assert status == 1
    assert "\r" not in err and "line one\\x0d\\x0aline two" in err


# --- 5.3 the real writer and the real entry point ---------------------------------------------

async def test_reads_the_real_writers_file_while_it_is_open(tmp_path, monkeypatch, capsys):
    log = audit.AuditLog(str(tmp_path))
    blocked = audit.blocked_record("python", "refused", "code is empty.")
    start = audit.start_record("ab" * 8, language="python", code="print(1)\n", timeout_s=10,
                               backend="native", runtime=None, verdict="native", image=None,
                               client=None)
    result = RunResult(stdout="1\n", stderr="", exit_code=0, timed_out=False, cpu_ms=31,
                       mem_peak_mb=11, denied_syscalls=["mount"], isolation="native")
    end = audit.end_from_result("ab" * 8, result, 84)
    assert await log.confirm(log.submit(blocked), log.submit(start), log.submit(end)) is None
    status, out, err = _run(monkeypatch, capsys, tmp_path)  # the writer still holds its fd
    assert (status, err) == (0, "")
    assert _rows(out) == [
        f"{blocked['ts']}  python  refused    code is empty.",
        f"{start['ts']}  python  ran        isolation=native exit=0 wall=84ms cpu=31ms "
        "mem_peak=11MiB denied=mount  print(1)",
    ]


def _script_env(directory) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VESTIBULE_")}
    env["VESTIBULE_AUDIT_DIR"] = str(directory)
    return env


def test_the_console_script_over_a_cp1252_pipe(tmp_path):
    _write(tmp_path, A, *_hostile())
    env = _script_env(tmp_path) | {"PYTHONIOENCODING": "cp1252"}
    p = subprocess.run([str(SERVER_EXE), "audit"], env=env, capture_output=True, timeout=60)
    assert (p.returncode, p.stderr) == (0, b"")
    assert max(p.stdout) < 0x80  # R4: cp1252 isn't UTF-8, so the output is ASCII
    out = p.stdout.decode("ascii").replace("\r\n", "\n")
    assert out.startswith(f"audit records in {tmp_path}\n")
    assert "\\x1b[8m\\u202eevil()..." in out and "print('\\u65e5\\u672c \\xe9')" in out
    assert out.endswith("3 of 3 calls, from 1 file\n")


@pytest.mark.skipif(os.name == "nt", reason="Windows raises OSError errno 22 there, left alone "
                                            "by design (plan §2.1)")
def test_a_reader_that_quits_early_gets_no_traceback(tmp_path):
    _write(tmp_path, A, *[r for i in range(5000) for r in (_start(str(i), i), _end(str(i), i))])
    p = subprocess.Popen([str(SERVER_EXE), "audit", "5000"], env=_script_env(tmp_path),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.stdout is not None and p.stderr is not None
    p.stdout.readline()  # about 550 KB follow: far more than the pipe holds
    p.stdout.close()
    err = p.stderr.read()
    assert p.wait(60) == 1
    assert b"Traceback" not in err and b"Exception ignored" not in err, err
