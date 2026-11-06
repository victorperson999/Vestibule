# M3 step 3 — `vestibule-mcp audit`

Status: **signed off 2026-11-04 (v2), R1–R5 included; built 2026-11-04 (§9 has what the build
and the review changed).** v2 was revised after the `skeptic` review: 12 findings, 2 medium and
10 low, verdict "sound with amendments". `verifier` checked the 11 claims they make and
confirmed every one, with one piece of reasoning only plausible (§8). §3.1 lists the five places
where the plan departs from the contract's wording; Victor approved each.

Inputs:
- the M3 contract, [`M3-audit-usage.md`](M3-audit-usage.md) (signed off 2026-11-01): A10,
  criterion 7, and §6's step 3, with W1's amendment to A6 (a `blocked` record carries an
  `outcome`);
- step 2's plan, [`M3-step2-audit-writer.md`](M3-step2-audit-writer.md): the records as built
  (§2.4) and the file name (§2.1);
- the code at `fb26ba1`;
- facts checked on 2026-11-04 (Windows 3.11.9 and 3.13.1, WSL 3.14):
  - reading and parsing 100 MB of records (20,000 calls in 4 files, each `start` holding 4 KiB
    of code) takes 0.43 s, so reading every file on every call is affordable;
  - `unicodedata.category`:
    - C0, DEL and C1 are `Cc`;
    - the contract's bidi controls, LRM, RLM, ALM, ZWNJ, ZWJ, U+FEFF, U+E0001 and U+E0020–E007F
      (the tag characters) are `Cf`;
    - U+2028 is `Zl`, U+2029 is `Zp`, and lone surrogates are `Cs`;
    - U+3164, U+115F and U+FFA0 are `Lo`, and U+2800 is `So`;
    - the set moves with the interpreter's Unicode version: 3.11 has 14.0 and 3.13 has 15.1, and
      U+13439 is `Cn` on one and `Cf` on the other;
  - when the reader of a pipe quits early, Python raises `BrokenPipeError` on POSIX, but
    `OSError: [Errno 22]` on Windows;
  - on Windows, `print` into a pipe writes `\r\n`;
  - mypy rejects `sys.stdout.reconfigure` (`sys.stdout` is `TextIO | MaybeNone` in typeshed)
    unless it's behind `isinstance(sys.stdout, io.TextIOWrapper)`.

---

## 1. Scope

**In:**
- **`vestibule-mcp audit [N]`** (A10). It prints the last N calls (default 20), oldest first,
  merged by `ts` across every audit file in the dir, one line per call.
- **One sanitizer** that every printed value from a record or the environment goes through, and
  a stdout that can't raise on any character (R4).
- **A shared helper in `config.py`** for turning `VESTIBULE_AUDIT_DIR` into a path, so the
  command and the server give the same messages (skeptic 4).
- **The README and GETTING_STARTED lines** the command needs (§6).

**Not in this step:**
- **Non-blocking stderr** (A11) is step 4.
- **The README's "Audit log" section,** the PLAN.md naming change (`vestibule --audit` →
  `vestibule-mcp audit`) and the live check (criterion 10) are step 5.
- **The writer doesn't change.** `audit.py` and the record format stay as `fb26ba1` has them.

---

## 2. What changes

### 2.1 The command (`src/vestibule/audit_cli.py`, new)

A module of its own, because it prints to stdout. `audit.py` promises that nothing in it writes
to stdout (golden rule 1), and the server imports this module only when the command runs (§2.3).

**Which dir.**
- The command reads `VESTIBULE_AUDIT_DIR` itself: its value, or the default when it's unset or
  blank, as `Limits.from_env` reads it. It never uses `server.LIMITS`, which falls back to the
  defaults when any other setting is invalid (`server.py:48–56`). No other setting is read.
  Reading needs only the dir, and the shell that runs `audit` usually doesn't have the MCP
  config's env anyway.
- The path comes from `config.audit_dir` (§2.2), the same helper the server's check uses:
  - **An explicit value it refuses:** stderr `vestibule audit: <its message>`, exit 1. It's the
    server's own config-error text, e.g. for a `~` value with a relative home, or (POSIX) a
    `~nosuchuser` that doesn't exist. The server never wrote there.
  - **The default it refuses** (no home directory, or a relative one): stderr `vestibule audit:
    could not find the home directory; set VESTIBULE_AUDIT_DIR` (the writer's `_NO_HOME`
    text), exit 1.

**Which files.**
- `os.scandir(dir)`, keeping regular files whose name matches the writer's pattern
  `[0-9]{8}T[0-9]{6}Z-[0-9]+\.jsonl`, in name order. Anything else in the dir is ignored.
- **The dir doesn't exist:** printed as "no records" (below), exit 0.
- **The dir can't be listed** (not a dir, no permission): stderr names it, exit 1.
- **A file that can't be opened or read** (deleted since the listing, no permission): stderr
  names it, the other files are still shown, and the exit status is 1.

**Reading** (skeptic 1).
- Binary mode, one record per `\n`-terminated line, `json.loads` per line, catching `ValueError`
  and `RecursionError`.
- **A usable record** is a JSON object with `type(v) is int and v == 1` (so `true` and `1.0`
  aren't), a `kind` of `blocked`, `start` or `end`, and `id` and `ts` of type `str`. Every other
  line is **skipped and counted**: a torn record, a blank line, text that isn't JSON, a record of
  another version or kind.
- **Each usable record is cut down as it's read** to what its row needs, and the cut-down can't
  raise:
  - a printed field is kept only when it's a `str`, or an `int` or `float` that isn't a
    `bool`. Anything else (a list, an object, a `bool` where a number belongs) becomes `?`, and
    a missing or null field becomes `-`;
  - `usage` is read only when it's an object, and `denied_syscalls` only when it's a list, from
    which only the `str` items are kept;
  - a `start`'s code becomes its row snippet (R2), so memory follows the number of calls, not
    the files' size.

  So `str()` only ever sees a string or a number: a deeply nested value never reaches it.

**Pairing.**
- One server process writes both records of a call, so pairing is per file.
- A `start` opens a call. An `end` closes the open call with its `id` in the same file. With no
  such call, the `end` is a call of its own: its `start` was lost, either torn or never written
  (a failed write leaves no line, and W2 queues the `start` without waiting). A `blocked` is a
  call of its own.

**Choosing and ordering.**
- Calls are sorted by the `ts` of their first record, then file name, then line number. So the
  order is total and the same on every run. A clock step can still misplace a call, as the
  contract's §4 accepts.
- The last N are printed, oldest first.

**The output (stdout).**

```
audit records in /home/v/.vestibule/audit
2026-11-03T21:07:03.512Z  python  ran        isolation=native exit=0 wall=84ms cpu=31ms mem_peak=11MiB  print(1)
2026-11-03T21:07:06.100Z  python  ran        isolation=native exit=1 wall=95ms cpu=40ms mem_peak=12MiB denied=mount  import ctypes...
2026-11-03T21:07:10.004Z  bash    timed_out  isolation=container exit=-1 wall=10204ms  while true; do :; done
2026-11-03T21:08:00.120Z  node    refused    wall=5003ms too many concurrent runs (max 4); retry shortly  console.log(1)
2026-11-03T21:08:05.000Z  python  refused    code is empty.
2026-11-03T21:08:09.300Z  python  cancelled  wall=1520ms  import time...
2026-11-03T21:09:00.777Z  python  no end     (no end record: still running, the server stopped, or the record was lost)  import os...
7 of 112 calls, from 3 files; 1 unreadable line skipped
```

- **The header** names the dir it read, after `expanduser`.
- **Each row** is `<ts>  <language>  <outcome>  <the rest>`. The language is padded to 6 and
  the outcome to 9.
  - **`ts`** is the call's first record's, exactly as written: UTC, to the millisecond. So a row
    can be found in the file with `grep`.
  - **`outcome`** is the `end`'s, or the `blocked` record's (W1). A `start` with no `end` shows
    `no end`.

  | Call | The rest |
  |---|---|
  | `end` with a result: `ran`, `timed_out`, `not_run` | `isolation=`, `exit=`, `wall=…ms`, then `cpu=…ms` and `mem_peak=…MiB` when measured, then `denied=` when the list isn't empty (R5), then the code |
  | `end` with `refused` or `failed` (R3) | `wall=…ms`, the reason, then the code |
  | `blocked` with `refused` or `failed` | the reason |
  | `blocked` with `cancelled` | `(cancelled before a backend was chosen)` |
  | `end` with `cancelled` | `wall=…ms`, then the code |
  | `start` with no `end` | `(no end record: still running, the server stopped, or the record was lost)`, then the code |
  | `end` with no `start` | its fields as above, then `(no start record found)` |

  One rule underneath: every call with a `start` ends with its code. `cpu` and `mem_peak` use
  `MiB`, as the result's `usage:` line does (step 1's U1).
- **The caps.** Every printed field is cut: 40 characters for `ts`, `language`, `outcome`,
  `isolation` and the numbers, 60 for the code and for `denied`, and 100 for the reason. A
  `...` marks each cut. So no hand-edited field can flood a row.
- **The summary line** comes last: `<shown> of <total> calls, from <files> files`, plus
  `; <k> unreadable lines skipped` and `; <f> files couldn't be read` when nonzero.
- **No records** (no matching files, none with a usable record, or no dir): the header, then
  `no records. If your MCP config sets VESTIBULE_AUDIT_DIR, set the same value here.` (A10's
  hint), plus the skip count if any. Exit 0.

**The snippet (R2),** for the code and the reason:
- the first line that isn't blank, after splitting on every line break `str.splitlines` knows,
  then stripped;
- `(blank)` when there's no such line. That's 5,000 spaces and then code: the record keeps
  only the first 4 KiB, and that's all spaces (skeptic 5);
- `...` after it when it was cut, when more lines follow, or when the record says
  `code_truncated`.

**The sanitizer (`_clean`, R1).**
- Every character whose Unicode category is `Cc`, `Cf`, `Cs`, `Zl` or `Zp` becomes `\xNN` below
  U+0100, `\uNNNN` below U+10000, and `\UNNNNNNNN` above. Everything else is kept, including
  letters in any script, emoji and the backslash.
- **It applies to every value from a record or the environment:** `ts`, `language`,
  `outcome`, `isolation`, the numbers, the code, the reason, the syscall names, and the dir path.
  Only the program's own text skips it.
- **The order is: choose the line, clean it, cut it.** So `\r` can't overwrite a row, and
  cutting an escape in half only shortens text.
- **Backslashes are not doubled.** Python code is full of `\n` inside strings, and `\\n` would
  misquote it. So a literal `\x1b` typed in the code and an escaped ESC look alike. The JSONL
  keeps the exact text.

**Encoding (R4).** `main` starts with `sys.stdout.reconfigure(errors="backslashreplace")`, behind
`isinstance(sys.stdout, io.TextIOWrapper)` for mypy (and for a `None` stdout). The stream keeps
its encoding:
- **A Windows pipe** (cp1252): characters outside cp1252 come out as `\uXXXX`, so nothing
  raises. That was the contract's measured failure.
- **A UTF-8 terminal,** which is nearly every terminal today, including the Windows console:
  unchanged, so `日本` prints as itself.
- **A POSIX terminal in a legacy 8-bit charset:** it gets bytes in its own charset. Every
  character it would read as a C1 control is `Cc`, which `_clean` has already escaped.

stderr keeps Python's default `backslashreplace`, and the values on it are cleaned anyway.

**A broken pipe** (skeptic 9). On POSIX, a `BrokenPipeError` while printing is answered as the
Python docs recommend: `os.dup2` a devnull fd over stdout, then exit 1. That way `audit 500 |
head` ends quietly. On Windows the same case raises `OSError` errno 22. That can't be told apart
from a real write error without guessing, so it's left alone, and Python prints its own
traceback. The default 20 rows (about 3 KB) fit any pipe's buffer.

**Arguments.** None, or one: `N` as 1 to 9 ASCII digits, greater than 0. Anything else prints
`usage: vestibule-mcp audit [N]  (N: how many calls to show, default 20)` to stderr, exit 2.

**Exit status.** 0 when everything was read, including when there are no records; 1 when the
dir setting, the dir or any file couldn't be used; 2 for bad arguments.

### 2.2 The shared helper (`config.py`)

Lines 175–186 of `_audit_problem` move into a public function, which `_audit_problem` then calls.
The server's behavior and messages don't change.

```python
def audit_dir(limits: Limits) -> tuple[Path | None, str | None]:
    """The audit dir as a path, or None and why VESTIBULE_AUDIT_DIR can't name one (A8)."""
    shown = f"VESTIBULE_AUDIT_DIR={_shown(limits.audit_dir)}"
    try:
        audit = limits.audit_path
    except RuntimeError as e:  # no home directory, or a ~user that doesn't exist
        return None, f"{shown} must be an absolute path or start with ~ ({e})"
    if not audit.is_absolute():  # relative, drive-relative (Windows), or a relative home
        if limits.audit_dir.startswith("~"):
            return None, (f"{shown} expands to {audit}, which isn't absolute: the home "
                          "directory is relative")
        return None, f"{shown} must be an absolute path or start with ~"
    return audit, None
```

`_audit_problem` becomes `audit, problem = audit_dir(limits)`, then `if audit is None: return
problem if explicit else None` (W4), then the overlap loop as today.

### 2.3 The dispatch (`server.py`)

```python
def run() -> None:
    args = sys.argv[1:]
    if args == ["setup"]:
        sys.exit(rootfs.setup_main())
    if args[:1] == ["audit"]:
        from vestibule import audit_cli  # a terminal command: its stdout is the user's
        sys.exit(audit_cli.main(args[1:]))
    if args:
        log.warning('ignoring unexpected arguments %s; the subcommands are "setup" and '
                    '"audit"', args)
    asyncio.run(_main())
```

`vestibule-mcp audit` goes through `server.py`'s import, as `setup` does. That import runs
`Limits.from_env`, whose overlap check calls `realpath` on the workspace, rootfs and audit dirs.
So `audit` shares step 2's known issue: it can hang on a dead hard-mounted NFS dir, even one it
never reads (skeptic 4). The import logs nothing.

---

## 3. Choices this plan makes

### 3.1 Departures from the contract's wording — for Victor's sign-off

| # | The contract says | This plan does | Why |
|---|---|---|---|
| R1 | A10: escape "C0 and C1 controls, DEL and the bidi controls (U+202A–202E, U+2066–2069) as `\x..`/`\u....`" | Escape every character in the categories `Cc`, `Cf`, `Cs`, `Zl` and `Zp`, with `\U........` above U+FFFF | A superset in one rule, simpler than a list of ranges. It also catches the other Trojan Source characters (LRM, RLM, ALM), the zero-width characters, and the tag characters (U+E0001, U+E0020–E007F), which can hide text from a human reader. `Cs` shows a lone surrogate from a hand-edited file as an escape, not `?`. The costs are below the table. |
| R2 | A10: "the code's first line, cut to fit" | The first line that isn't blank, stripped and cut to 60 characters, or `(blank)`. `...` marks a cut, more lines, or `code_truncated`. The reason gets the same rule at 100 | Usability: code that starts with a blank line, or with `\r`, would otherwise show an empty snippet. It isn't a security measure: a comment as the first line hides the first statement just as well (skeptic 5). A fixed width keeps the output the same in every terminal and the tests exact; a narrow terminal wraps. |
| R3 | A10: "A `blocked` or `refused` call gets its reason instead" | A call with a reason (`refused` or `failed`) shows it. A call with a `start` also shows its `wall` and its code | `end(failed)` is what the outer deadline writes (`server.py:245–249`), after the run hung for up to timeout + 30 s. The guest has probably run, so its code is the thing to look at. A10 predates W1, which gave `blocked` its `failed` outcome (skeptic 2). |
| R4 | A10: "stdout is reconfigured as UTF-8 with `errors="replace"`" | stdout keeps its encoding, with `errors="backslashreplace"` | It fixes the measured cp1252 crash with one setting and no encoding change. Forcing UTF-8 would send a legacy 8-bit POSIX terminal bytes it reads as C1 controls: U+00DB encodes as `C3 9B`, and 0x9B is CSI (skeptic 7). On a Windows pipe, characters outside cp1252 then show as `\uXXXX` instead of UTF-8. |
| R5 | A10's row: time, language, outcome, isolation, exit code, wall, cpu, mem_peak, code | Also `denied=<names>` when an `end` holds denied syscalls | PLAN.md:88 lists denied syscalls in the audit trail, and `end` records keep them (`audit.py:236`). They're the one per-run fact that signals an escape attempt (`mount`, `bpf`). Two lines (skeptic 11). |

**R1's costs,** for the contract's amendment:
- U+200C (ZWNJ) is routine inside Persian words, which will show `\u200c`. An emoji joined
  with U+200D shows that escape between its parts.
- The set follows the interpreter's Unicode version (§ Inputs), so a newly assigned format
  character is escaped only on a newer Python.
- Invisible letters stay visible-as-blank: U+3164, U+115F, U+FFA0 (`Lo`), U+2800 (`So`).
- A real U+202E and the six typed characters of its escape print the same (backslashes aren't
  doubled).

### 3.2 Fixed choices

- **Pairing is per file** (§2.1): a process writes both records of a call.
- **The row's `ts` is the record's,** not local time: exact, and greppable.
- **The orphan rows name every cause they could have,** not one guess (skeptic 3).
- **No terminal-width logic, no colors, no flags** (A10).
- **No line-length cap on reading.** The writer bounds a record at about 35 KB (4 KiB of code
  escaped 6× at worst, plus the rest). Only the user can write anything longer into their own
  0700 dir, and A12 already says the user can edit the audit. The printed fields are capped
  (§2.1).

---

## 4. Edge cases

| Case | Behavior |
|---|---|
| A live server is writing the newest file | Read as it is. A record mid-write shows as one unreadable line until the write finishes |
| The server holds the file open (Windows) | Both `os.open` and `open()` share for read and write, so it can be read; test 13 checks it |
| A torn record, then newline repair (step 2) | One unreadable line; the next record parses |
| A `start` whose `end` is torn, or was never written | `no end` row |
| An `end` whose `start` was never written (a failed write, a full queue) | Its own row, `(no start record found)`, and nothing counted as unreadable |
| Two `start`s with one `id` in a file (hand edit) | The second opens a new call; the `end` closes the second; the first shows `no end` |
| `ts` that isn't ISO (hand edit) | Sorted as a string; never parsed, so it can't raise |
| A field of the wrong type, or nested 980 deep | `?` (§2.1); it never reaches `str()` |
| Fewer than N calls | All of them |
| A file named like an audit file but a directory | Skipped (`is_file()`) |
| Non-ASCII code (`print("日本")`) | Printed as itself on a UTF-8 terminal; `\u65e5\u672c` on a cp1252 pipe (R4) |
| A dir path holding surrogate escapes (POSIX bytes) | `\udcXX` (R1's `Cs`) |

---

## 5. Tests

All in `tests/test_audit_cli.py` (new) and on every platform unless marked. Assertions about
characters are made on the decoded text, not on bytes: a legitimate `Û` encodes as `C3 9B` in
UTF-8 (verifier 7).

### 5.1 Pure

1. **`_clean`, one table.**
   - Escaped, each to its exact escape: NUL, TAB, LF, CR, ESC, DEL, U+0085, U+009B (CSI), U+009D
     (OSC), each of U+202A–202E and U+2066–2069, U+200E, U+200B, U+061C, U+FEFF, U+2028, U+2029,
     U+E0041 and U+D800.
   - Kept: `héllo wörld 日本 😀`, a backslash, and a backslash followed by `x1b` as typed.
2. **The snippet (R2).**
   - A code of LF, LF, `foo`, LF, `bar` gives `foo...`.
   - `a` CR `b` gives `a...`, and so do `a` U+2028 `b` and `a` U+001C `b`.
   - One short line has no `...`, and a 61-character line is cut to 60 plus `...`.
   - Leading spaces are stripped.
   - 4,096 spaces with `code_truncated` give `(blank)...`.
   - One line with `code_truncated` gets `...`.
3. **Arguments.** `[]` shows 20 of 25 calls, and `["3"]` shows 3. `["0"]`, `["-1"]`, `["abc"]`,
   `["²"]`, `["1", "2"]` and `["1234567890"]` each exit 2, with the usage line on stderr and
   nothing on stdout.

### 5.2 Over files (hand-written JSONL in `tmp_path`)

4. **Two processes' files merged.**
   - Records interleave across files: A1 < B1 < A2 < B2 by `ts`, with A's file name sorting
     first (skeptic 8).
   - The last N are chosen and printed oldest first.
   - Equal `ts` in two files are ordered by file name.
   - One `id` is reused across the two files, and each `end` pairs with its own file's `start`.
5. **One exact-output test** for a small dir holding every row kind in §2.1's table, including
   `denied=` (R5) and an `end(failed)` with its code (R3). It pins the format.
6. **Unreadable lines, each counted:**
   - a torn line mid-file, a blank line, and a JSON array;
   - a record without `ts`, one with a numeric `id`, one with `kind: "other"`;
   - records with `v: 2`, `v: true` and `v: 1.0`;
   - 100,000 nested `[`.

   The summary says `10 unreadable lines skipped`, and the rows around them are shown. A
   second case swaps in a `json.loads` that raises `RecursionError`, so mutant 10 doesn't depend
   on an interpreter's depth limit.
7. **Wrong field types in usable records** (skeptic 1): `usage: 5`, a `reason` nested 985 deep,
   `exit_code: {"a": 1}`, `denied_syscalls: "mount"`, and a 30 KB `isolation`. Each row shows
   `?` or a capped field, and nothing raises.
8. **Orphans.** A `start` with no `end`, and an `end` with no `start` and no unreadable line
   around it (the write that never landed).
9. **A hostile record** (criterion 7).
   - The code holds `\x1b[8m`, CR and U+202E; the reason holds ESC and OSC 52.
   - `ts`, `language`, `outcome`, `isolation` and `wall_ms` each hold ESC, and so does a syscall
     name.
   - The decoded output holds no character in R1's categories other than the row ends, and shows
     each one escaped.
10. **cp1252 stdout** (criterion 7's "piped on Windows", on every platform). `sys.stdout` is
    replaced by a `TextIOWrapper` over a `BytesIO` with `encoding="cp1252"`.
    - `main` doesn't raise.
    - The bytes decode as cp1252, holding the escapes, `é` as itself, and `日本` as
      `\u65e5\u672c` (R4).
11. **The dir.**
    - Missing gives the hint, exit 0, and so does empty.
    - A file instead of a dir exits 1.
    - A stray `notes.jsonl` and a dir named like an audit file are both ignored.
    - A file whose `open` raises `PermissionError` (monkeypatched) is named on stderr, the other
      file is still shown, and the exit status is 1.
    - A nonexistent dir whose name holds ESC, set through the env, prints the header with
      `\x1b`.
12. **The setting.**
    - Unset or blank: `HOME` and `USERPROFILE` point at `tmp_path`, and the header shows
      `tmp_path/.vestibule/audit`. It never reads the real dir (skeptic 8).
    - The default under `NoHome` and under `RelativeHome` (`tests/homeutil.py`) gives the home
      message, exit 1.
    - Explicit values, each with the server's own text, exit 1:
      - `~/x` under `RelativeHome` gives "expands to …";
      - `~/x` under `NoHome` gives "must be an absolute path or start with ~ (…)";
      - a plain relative value gives "must be an absolute path or start with ~".
    - `tests/test_config.py`'s existing audit-dir tests stay green unchanged. That shows §2.2
      didn't change the server.

### 5.3 Against the real writer and the real entry point

13. **Round trip.** A real `AuditLog` in `tmp_path` writes a `blocked`, and a `start` with its
    `end_from_result`, through the step-2 builders. `main` reads them **while the writer still
    holds its fd**, which is the Windows sharing case, and the rows match. This ties the reader
    to the writer's real file name and fields.
14. **A real pipe.** `tests/stdioutil.SERVER_EXE audit` runs, which is the console script, so a
    broken `[project.scripts]` entry fails (skeptic 8).
    - The env has `PYTHONIOENCODING=cp1252`, `VESTIBULE_AUDIT_DIR` set to a dir holding test
      9's record, and the dev shell's other `VESTIBULE_*` removed.
    - Exit 0, stderr empty.
    - stdout, with `\r\n` normalized, decodes as cp1252 and holds the escapes.
15. **A broken pipe** (POSIX; Windows behaves differently by design, §2.1). The console script
    runs `audit 5000` over 5,000 calls, so its output overflows the pipe. The test reads one
    line and closes the pipe. The exit status is 1, with no `Traceback` and no `Exception
    ignored` on stderr.
16. **The dispatch** (`tests/test_rootfs.py`, beside the `setup` tests). `vestibule-mcp audit 7`
    calls `audit_cli.main(["7"])` and exits with its status. The existing unexpected-arguments
    test gets the new warning text.

v1's test over real stdio is dropped. Tests 13 and 14 already tie the command to the real
writer and the real entry point, and the long e2e test shouldn't depend on this output format
(skeptic 12).

### 5.4 Mutants (each must turn a test red; a green control run first)

1. `_clean` keeps `Cf` → 1, 9.
2. `_clean` keeps C1 (`Cc` limited to below U+0080) → 1, 9.
3. `_clean` uses `\u` for everything (no `\U`) → 1.
4. The snippet split on `\n` only → 2, 9.
5. Blank first lines not skipped → 2.
6. No `...` when more lines follow, or when `code_truncated` → 2.
7. Sorted by file order, not `ts` → 4.
8. The first N, not the last → 3, 4.
9. Printed newest first → 4.
10. Only `ValueError` caught → 6 (the stand-in `json.loads`).
11. The skip count left out of the summary → 6.
12. `v == 1` instead of `type(v) is int` → 6.
13. A printed field taken whatever its type → 7.
14. A field cap removed → 7.
15. No `reconfigure` → 10, 14.
16. A `start` with no `end` dropped → 8.
17. `N == 0` accepted → 3.
18. The file pattern widened to `*.jsonl` → 11.
19. The reason not cleaned → 9.
20. Pairing across files instead of per file → 4.
21. `end(failed)` without its code (R3) → 5.
22. `denied=` left out (R5) → 5.
23. The CLI reads `server.LIMITS.audit_dir` → 12 (the HOME case).
24. The `BrokenPipeError` handler removed → 15 (in WSL).

### 5.5 The gate

The full gate: Windows, then WSL on 3.14 and 3.11, then stdio.

---

## 6. Docs (this step)

- **README:**
  - one bullet after "Then ask your agent to run some code", with the venv path the README
    already uses (`README.md:75`): `.venv/bin/vestibule-mcp audit` (Windows:
    `.venv\Scripts\vestibule-mcp audit`). It prints the last 20 calls: time, language, outcome,
    isolation, exit code, usage, denied syscalls and the code's first line. `audit 50` prints
    the last 50 (skeptic 10);
  - the `VESTIBULE_AUDIT_DIR` row: if your MCP config sets it, set it for `audit` too.
- **GETTING_STARTED, troubleshooting:**
  - `audit` says `no records`: the setting must match the MCP config;
  - `unreadable lines skipped`: a record torn by a full disk or a crash, or a hand edit;
  - PowerShell 5.1 pipelines (`| Select-String`) can garble non-ASCII characters; the escapes
    are ASCII, so nothing hidden gets through.
- **ARCHITECTURE:** two sentences after the writer's paragraph: the reader merges the files by
  `ts`, and every printed value goes through one sanitizer.
- **The contract** (`M3-audit-usage.md`): an amendment blockquote under A10 for R1–R5, with R1's
  costs, once you sign them off.
- **At done:** HISTORY bullets (the sign-off, then done), a BUILD_LOG entry, and this plan's
  as-built section.

---

## 7. Files and size

| File | Change | Rough size |
|---|---|---|
| `src/vestibule/audit_cli.py` | new: dir, files, reading, typed cut-down, pairing, rows, snippet, `_clean`, broken pipe, `main` | ~200 |
| `src/vestibule/config.py` | `audit_dir` out of `_audit_problem` | ~6 net |
| `src/vestibule/server.py` | the dispatch and the warning text | ~4 |
| `tests/test_audit_cli.py` | new: tests 1–15 | ~400 |
| `tests/test_rootfs.py` | test 16 and the warning text | ~12 |
| `README.md`, `docs/GETTING_STARTED.md`, `docs/ARCHITECTURE.md` | §6 | ~15 |

src ~210, against the contract's ~110. The additions are the review's: the typed cut-down and
caps, R3's and R5's fields, the orphan rows, the broken-pipe recipe, and the exit status for an
unreadable file. Tests ~410, against its ~140. Step 2 came in at about twice its test estimate,
so this one is set from §5's cases, not from the contract's figure.

---

## 8. Review (`skeptic`, then `verifier`, 2026-11-04)

`skeptic`'s verdict was "sound with amendments", with 12 findings (2 medium, 10 low). `verifier`
checked the 11 claims they make. All 11 were confirmed, with corrections to some details, and
none refuted. One piece of reasoning is only plausible: that the guest probably ran behind an
`end(failed)`. That holds for the outer deadline, not for an unexpected internal error, which
the same `except` catches. Every finding is taken.

Verifier corrections that shaped v2:
- **1:** a nested value parses up to about depth 994 on 3.11, and `str()` raises from about 980.
  On 3.13 neither raises at 1000. The crash comes at print time, outside any reading guard, so v2
  types the fields instead of widening the guard.
- **3:** the lost `start` or `end` isn't only a cancel's: any failed write, or a full queue,
  gives it.
- **4b:** `~nosuchuser` raises only on POSIX; on Windows it expands to `C:\Users\nosuchuser`.
- **6:** U+E0000 itself and U+E0002–E001F are `Cn`; the tag characters proper are `Cf`.
- **7:** a byte-level "no C1" check would flag a legitimate `Û`, so the tests check decoded text.

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | med | Only `json.loads` was guarded: a valid record with a nested `reason`, `usage: 5` or a 30 KB field crashes or floods the command; `v == 1` accepts `true` and `1.0` | Typed cut-down (only `str` and non-`bool` numbers printed, else `?`), caps on every field, `type(v) is int`; tests 6 and 7; mutants 12–14 |
| 2 | med | An `end(failed)` row hid the code and wall of a run that probably executed (the outer deadline) | R3: every call with a `start` shows its code, and `refused`/`failed` ends show `wall` and the reason too |
| 3 | low | The orphan messages claimed one cause; a failed or dropped write is another | Neutral wording naming every cause (§2.1, §3.2); test 8 |
| 4 | low | Copying `_audit_problem`'s rules would bring back step 2's finding 2.4, and mis-answer `~nosuchuser`; `audit` inherits the NFS hang at import | The shared `config.audit_dir` (§2.2); test 12; §2.3 states the hang |
| 5 | low | R2's security reason doesn't hold; 5,000 spaces then code gives an empty snippet | R2 reworded as usability; `(blank)`; `...` on `code_truncated`; test 2 |
| 6 | low | R1's costs weren't stated (ZWNJ, Unicode version drift, invisible letters, escape look-alikes); a wrong fact about U+E0000; raw U+200D and U+2028 in v1's own text | Listed under R1 for the amendment; the fact corrected; v2's text written with escapes only |
| 7 | low | mypy rejects a bare `sys.stdout.reconfigure`; forced UTF-8 can feed a legacy 8-bit terminal C1 bytes | The `isinstance` guard; R4 (keep the encoding, `backslashreplace`) |
| 8 | low | Weak tests: test 4 not interleaved, test 13 skipped the console script and didn't allow `\r\n`, a blank setting read the real dir, fields cleaned without a test | Tests 4, 9, 12 and 14 as revised |
| 9 | low | "Not worth it" was wrong on POSIX, where `BrokenPipeError` is its own class | The docs' recipe on POSIX; test 15; Windows left alone and stated |
| 10 | low | A bare `vestibule-mcp audit` isn't on PATH after the README's install; PowerShell pipes garble non-ASCII | §6's venv paths and GETTING_STARTED line |
| 11 | low | Denied syscalls, the one escape-attempt signal, weren't in the row | R5 |
| 12 | low | Simplicity: the e2e stdio test added little; the size estimate was low | Test dropped; §7 re-estimated |

---

## 9. As built (2026-11-04)

Built as §2–§6 say, except for the changes below. One came from the build; the rest came from
the two review rounds.

**From the build:**
- **Test 7's nested `reason` is 500 deep, not 985.** Under pytest on 3.11, `json.loads` stops
  near depth 955, because pytest's own frames count toward the limit. So a line 985 deep is an
  unreadable one, not a usable record (verifier, round 2). The test needs a list where a string
  belongs, at any depth, and the typed cut-down never hands one to `str()`.
- **3.14 checked:** 100,000 nested `[` raises `RecursionError` there too, as test 6 needs.
- **`_count`** writes "1 file" and "2 files".

**From the first gate run:** Linux 3.14 had one teardown error in
`test_native.py::test_a_helper_that_reports_nothing_claims_nothing`. A scope was left behind
after a stand-in helper that exits before placement. The test passed 20 of 20 on its own, the
scope was gone afterwards, and the later gate runs were clean. It's a timing flake in a native
test this step doesn't touch.

**Review.** `critic` ran twice, each round checked by `verifier`.
- **Round 1:** 2 medium and 4 low findings on the code, all confirmed; `verifier` lowered one
  medium to low. A seventh noted that the docs weren't written yet, which is the loop's next step.
- **Round 2,** on the fixes: 1 medium and 9 low, all confirmed; `verifier` lowered the medium to
  low. Its fixes stayed within the findings' own lines, so there was no third round.

| # | Sev. | Finding | Done |
|---|---|---|---|
| 1.1 | low (critic: med) | R4's "every character a legacy 8-bit terminal reads as a C1 control is `Cc`" is false. KOI8-R puts `⌡`, `²` and `°` on 0x9B, 0x9D and 0x9C (CSI, OSC, ST), and cp1252 puts `›` on 0x9B | **R4 tightened:** a UTF-8 stream stays UTF-8; any other encoding becomes ASCII with `backslashreplace`, on stdout and stderr. A cp1252 pipe now shows `é` as `\xe9`. Tests: cp1252 and KOI8-R stand-ins for stdout, KOI8-R for stderr; mutants 15, 26 and 27 |
| 1.2 | med | The 60- and 100-character caps were never exercised through `main` | A test with an 80-character code line, a 139-character `denied` list and a 150-character reason; mutant 31 |
| 1.3 | low | Every record's whole first line was cleaned before the cut: about 9 s at 20,000 one-line 4 KiB starts | Only the first `cap + 1` characters are cleaned. Cleaning only lengthens text, so the output is the same (`verifier` fuzzed 200,000 cases) |
| 1.4 | low | `flush()` raised on a `None` stdout (`audit >&-`) | Guarded; test and mutant 29 |
| 1.5 | low | No test for `_clean` on the explicit-setting message | A relative home holding ESC; mutant 30 |
| 1.6 | low | The command read a dir the server would refuse as overlapping the workspace or the rootfs, and showed its files as genuine | **Added:** it also reads `VESTIBULE_WORKSPACE` and `VESTIBULE_ROOTFS_DIR` (the defaults when unset) and warns on stderr. It doesn't refuse, since the MCP config may move the workspace elsewhere. §2.1 said no other setting is read |
| 2.1 | low (critic: med) | The two new env reads had no test | One test per setting; mutants 34 and 35 |
| 2.2 | low | The warning said the records "may be forged" because sandboxed code "can write there", but native mounts the rootfs read-only | Neutral wording: "The server refuses that setting, so the records there may not be its own" |
| 2.3 | low | `is_file()` followed symlinks inside the one `try` around the listing, so one looping symlink stopped the whole command | `is_file(follow_symlinks=False)`, since the writer never makes one. A test with a self-referencing symlink; mutant 32 |
| 2.4 | low | With a `None` stderr, `print(file=None)` wrote errors to stdout | `_err` checks first; test and mutant 33 |
| 2.5 | low | No test for `_clean` on the overlap warning | The per-setting tests use a path holding ESC; mutant 36 |
| 2.6 | low | No test for `_clean` on the two `OSError` messages, and a Windows system message can hold CR LF | One test each; mutants 37 and 38 |
| 2.7 | low | This plan still described the behavior before the review | This section |
| 2.8 | low | A raw `日` in GETTING_STARTED where its escape was meant | Fixed. `verifier` scanned the step's files and found no other case |
| 2.9 | low | Simplicity: the warning and the two env reads aren't in the signed plan | Kept, with 2.1, 2.2 and 2.5 fixed. Victor decides (below) |
| 2.10 | info | Test 7's depth differs from §5 | Above: 500 is a depth that parses under pytest on 3.11 |

**For Victor's sign-off:** two review changes to what was approved, R4 as tightened (1.1) and
the warning (1.6, 2.9). Removing the warning means four lines in `main` and its three tests.

**Mutants.** All 39 turned a test red, each with exit status 1, after a green control. Every
touched file was restored byte for byte and checked against its hash.
- §5.4's 24, with 15 and 23 reworked for the new encoding rule and settings lines;
- 2 more on the build: `code_truncated`'s `...`, and `_audit_problem`'s W4 branch after the
  refactor;
- 13 for the review's fixes (26–38).

38 ran on Windows, and the broken-pipe handler (24) ran in WSL. The harness lives in the session
scratchpad only.

**Gate (after round 2's fixes):**
- check_env, ruff and mypy clean on both platforms.
- Windows: 620 passed, 141 POSIX/Linux-only skips.
- Linux, Python 3.14: 781 passed, 0 skipped.
- Linux, Python 3.11 Docker-free: 738.
- stdio: 5 of 5.

**Size,** against §7's estimates:
- **src:** `audit_cli.py` is 287 lines (estimate ~200). `config.py` changed by +19/−12 and
  `server.py` by +8/−2. The difference is the review's fixes (the encoding rule, the warning
  and its settings, the symlink, `None` stream and prefix-cleaning guards) and the docstrings.
- **tests:** `test_audit_cli.py` is 553 lines and `test_rootfs.py` changed by +12/−2, against
  ~410: the two review rounds added 13 tests.
