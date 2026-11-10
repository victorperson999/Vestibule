# M4 step 4 fix — `read_workspace` can return a file from outside the workspace

Status: **v2, signed off 2026-11-09 with option (1) on each of X1–X3. Built 2026-11-09; §10 is
the as-built, and it changes §2.2, §2.4, §4 and §8.** v1 was reviewed by `skeptic` (12 findings: 4
medium, 8 low; verdict "sound with amendments"; it found no path where v1's code returns outside
content). `verifier` then ran seven of its claims: six confirmed, each with a correction, and one
half confirmed and half plausible (§9). Every finding is taken.

This is the small step that V4 of [`M4-step4-docs.md`](M4-step4-docs.md) asked for, signed by
Victor on 2026-11-09 with option (1): fix the race before the release, with its own plan and the
full loop. It lands before 4a, so that `SECURITY.md` can say what holds. It amends M1's contract
§6 ([`M1-container-backend.md`](../M1/M1-container-backend.md)), which accepted the race as a
residual. **X3 below changes the Windows half of what V4's sentence described, and needs your OK.**

## 1. The problem, measured

`read_workspace_entry` (`src/vestibule/workspace.py`) checks a path, then opens it by name:
1. `resolve_in_workspace` calls `lstat` on each component and refuses a symlink or reparse point.
2. It calls `lstat` on the whole path once more.
3. It opens the whole path (`os.open`), or lists it (`os.scandir`).

The kernel resolves the name again in step 3. A guest that is running at the same time can
rename a folder away and put a link in its place between the check and the open. `O_NOFOLLOW`
guards only the last component, and Windows has no such flag in `os.open`.

**Measured on today's code** (2026-11-09; a scratch spike, not committed). The workspace has
`sub/nested.txt`. A folder outside it has its own `nested.txt` holding a marker.

- **A swap at a chosen moment.** A wrapper counts the module's filesystem calls and swaps `sub`
  for a link to the outside folder just before call number k, for every k.

  | | Reading `sub/nested.txt` | Listing `sub` |
  |---|---|---|
  | WSL, 3.14 and 3.11 | the outside file comes back at 3 of 8 call points | the outside listing at 1 of 7 |
  | Windows, 3.11 (a junction) | at 3 of 4 | at 1 of 3 |

- **A timed race,** 4 s, the swap made by a second thread of the same process:
  - WSL: 4,556 of 55,759 reads returned the outside file, and 9 of 65,342 listings.
  - Windows: 2 of 11,719 reads, and 0 of 14,007 listings.
  - `verifier` saw about 1.5% on WSL with its own setup. The rate depends on the swapper.
- **A real guest wasn't run** yet. §6 runs one.

**Why it matters:** the result goes to the model. A prompt-injected run could plant the swap
loop, then get the agent to read a path, and bring a host file such as `~/.ssh/id_ed25519` into
the agent's context. From there a later run can print it, or the agent can send it anywhere.

**Three more effects of the same check-then-open** (each run by `verifier`, §9):
- **The walk itself answers questions about host paths** (A). Its `lstat` calls go by name too.
  Through a swapped link, a call for `sub/<name>/z` comes back as "cannot inspect 'z': Not a
  directory" when the outside `<name>` exists and isn't a folder, and almost never otherwise: in
  a blind 2 s race, 23,131 of 47,707 calls against 0. The agent learns that a host file exists.
- **A hang, POSIX only** (F). A guest can swap a regular file for a FIFO after the check.
  `os.open` then blocks until something opens the FIFO for writing, and the tool call never
  returns. Its worker thread comes from the pool that native placement and the container
  backend's cleanup also use (16 threads on this machine). With all 16 stuck, a native run came
  back `native-degraded` with no CPU, memory or process limit, reported honestly, and an innocent
  `read_workspace` got no answer.
- **A 45 s stall, Linux** (D). A guest can take a write lease on its own file. Today's open then
  waits for the lease to break: 45.01 s measured, per call.

## 2. The fix

**One rule on every platform: open each name once, from the folder already held open, and never
follow a link.** The check-then-open goes away, and with it the by-name walk.

`read_workspace_entry` becomes:
1. `split_relpath`, unchanged: the lexical checks, before any filesystem call.
2. Resolve the workspace root and open it as a directory. The guest can't rename its own mount
   point, so this one open by name is safe, as today.
3. For each component, open it **relative to the directory just opened**. A link is refused. A
   missing name is `Not found: …`.
4. Ask the last descriptor what it is. A directory is listed through it, a regular file is read
   through it, and anything else is refused.

A link swapped in before its open is refused. A swap made after the open changes nothing: the
descriptor still points at the real folder. No name is resolved twice, and no question is asked
by name through a folder the guest controls.

### 2.1 POSIX

- **Per component:** `os.stat(name, dir_fd=fd, follow_symlinks=False)`, then
  `os.open(name, flags, dir_fd=fd)`.
  - The `stat` is there for the message only: it lets a link get today's "path traverses a
    symlink or reparse point; refused". The open's `O_NOFOLLOW` is what enforces it.
  - **Flags:** `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK`, plus `O_DIRECTORY` on every
    component but the last.
- **`O_NONBLOCK`** makes a FIFO open at once. The type check (`fstat`) comes before any read, so
  a FIFO is refused without being read. For a regular file the descriptor is set back to blocking
  before the read.
- **A directory** is listed with `os.scandir(fd)`. **A file** is read with today's buffered read.
- Every descriptor is closed in a `finally`.
- **Measured** (WSL, kernel 6.6, 3.14 and 3.11): `O_NOFOLLOW | O_DIRECTORY` on a symlink fails
  with `ENOTDIR`, plain `O_NOFOLLOW` with `ELOOP`, and `os.scandir` accepts a descriptor.
- `src/vestibule/unpack.py` already walks this way for the rootfs layers (M2 step 2). It raises
  its own error type and creates as it goes, so the few lines of walk are written again here.

**Considered and not taken:** `openat2` with `RESOLVE_BENEATH` does this in one call, but it's
Linux-only and has no `os` wrapper, so macOS would need a second path.

### 2.2 Windows (X3)

The same walk, through `ctypes`:
- **The root:** `CreateFileW` with `FILE_FLAG_BACKUP_SEMANTICS` (a directory needs it).
- **Per component:** `NtOpenFile` (in `ntdll`), which takes a directory handle and a name inside
  it. Options: `FILE_OPEN_REPARSE_POINT`, so a link is opened as itself and never followed;
  `FILE_DIRECTORY_FILE` on every component but the last; case-insensitive, as Windows paths are
  today. All sharing is allowed, so the guest's own writes aren't blocked.
- **After each open,** the handle's attributes are read (`GetFileInformationByHandleEx`). A
  reparse point, which covers symlinks and junctions, is refused there.
- **A directory** is listed through its handle (`GetFileInformationByHandleEx`, X1). **A file**
  is read through its handle (`msvcrt.open_osfhandle`, then the same read as POSIX).

**Measured** with the second prototype (Windows 11, 3.11.9):
- a junction swapped in at any call point is refused, and the 4 s race gave 0 leaks in 21,165
  calls;
- reads, listings, an empty folder, a 400-entry folder (more than one buffer of records), a
  non-ASCII name and a name in the other case all work;
- a link to another folder of the workspace is refused, as today.

**About `NtOpenFile`:** it is the system call that every Win32 open ends in. Microsoft documents
it, with the standard note that native calls may change between Windows versions. It has been
stable for decades, and nothing else in Win32 opens a name relative to a directory handle. It
needs three small structures declared.

### 2.3 What changes for a path nobody is racing

| Case | Today | After |
|---|---|---|
| A path that goes through a regular file (`hello.txt/x`) | POSIX: refused, "cannot inspect 'x': Not a directory". Windows: `Not found` | Both: refused, "cannot open 'hello.txt': …" |
| A file the server can't read (a guest's `chmod 000`) | refused, "cannot open file: Permission denied" | refused, "cannot open '<name>': Permission denied" |
| A folder with search permission but no read permission (`chmod 111`), POSIX | a file inside it can be read | refused: the walk opens each folder for reading |
| A path longer than 260 characters, Windows | works only where long paths are turned on | works: only the root is opened by its full name |
| Everything else | | the same answer and the same message |

No test or doc depends on the messages that change (`skeptic` searched).

### 2.4 What is removed, and what isn't touched

- **Removed:** `resolve_in_workspace` and `_is_symlink_or_reparse`. After this change nothing in
  `src/` calls them. The `commonpath` check goes with them: a walk that can't leave has nothing to
  compare. `test_jail_rejects` calls `read_workspace_entry` instead, which is the function the
  server calls.
- **Not touched:** `split_relpath`, the listing's format and its 200-entry cap, the truncation
  marker, `WorkspacePathError`, the handler in `server.py`, and both tool descriptions. The
  description's last sentence (a guest can change the workspace while this tool runs) stays true:
  a file's content can still change during a read.
- No new file, no new dependency, no new setting.

## 3. Edge cases

| Case | What happens |
|---|---|
| The path is `.` | The root's own descriptor or handle is listed |
| A component disappears mid-walk | `Not found: …` |
| A folder is swapped for a regular file mid-walk | Refused: not a directory |
| The last component becomes a FIFO, socket or device (POSIX) | Opened without blocking, or the open fails. Either way refused, and nothing is read |
| A guest holds a FIFO open and writes nothing | Still refused at once: the type check comes before any read |
| The file is replaced by another regular file mid-read | The descriptor keeps the old file. Content can be torn if it's rewritten in place, as today |
| Hard links | A guest can't link to a file outside its mounts, so every name it can make is a workspace file |
| A guest holds a write lease on its own file (Linux) | Refused at once, "Resource temporarily unavailable", where today's open waits 45 s (§1). So a guest can make its own file unreadable to this tool for as long as it runs, and nothing else |
| A workspace at the root of a drive (`W:\`) | Works: no path strings are compared. An empty root has no `.` entries, so its first listing call may fail with "file not found" where a folder's ends with "no more files". "File not found" on the first call is taken as empty. `verifier` got that error from NTFS for a first query that matches nothing, and couldn't make an empty drive to run the real case (§9, G) |
| A name Windows can't open (`a*b`, longer than 255) | Refused with the OS's reason |
| A huge folder | As today, every entry is read before the listing is cut to 200, so a guest's million files make one call slow and large. Not changed here. Recorded as a known issue |
| macOS | Takes the POSIX path: `O_NOFOLLOW`, `dir_fd` and `scandir(fd)` exist there. Run only by the manual job (§6, step 8) |

## 4. Tests (`tests/test_workspace.py`; red first; both platforms)

**The swap helper.** A stand-in for the module's `os` counts every call the module makes and
runs a swap just before call number k. When the module has `_k32` and `_nt` (the two `ctypes`
libraries of §2.2), they get the same counting wrapper, so every Windows call is a call point
too. The test loops over k until the swap no longer fires. A folder link is a symlink on POSIX
and a junction on Windows (`_winapi.CreateJunction`, which needs no privilege), so none of these
skips on Linux.

1. **`test_a_folder_swapped_at_any_moment_never_leaks`,** for `sub/nested.txt`, `sub`, and
   `a/b/c.txt` with the swap at `a` and at `a/b`. The link points at an outside folder, and in a
   second round at another folder of the workspace.
   - **Asserted for every k:** the result holds neither the other folder's marker nor a file name
     that only it has.
   - **Asserted about the helper:** it saw one relative open per component, the k just before each
     of them was refused, and at least one later k returned the real content. A later change that
     hides the opens from the stand-in fails here, not passes.
   - **Red on today's code** at the call points of §1.
2. **`test_a_file_swapped_for_a_link_is_refused`:** `hello.txt` swapped for a link to an outside
   file, and for a link to another workspace file. Every k where the swap came before the open is
   a refusal, not empty content and not a target's content.
   - The file link needs the symlink privilege on Windows (`_symlink_or_skip`, as the existing
     tests do).
   - **Red on today's code on Windows only.** On POSIX today's `O_NOFOLLOW` already refuses a
     swapped last component, so there it's green before and after.
3. **`test_a_file_swapped_for_a_fifo_cant_hang_the_read`** (POSIX; skipped on Windows, which has
   none), in two forms: a FIFO nobody has open, and one the test holds open for reading and
   writing while it writes nothing. The call runs in a thread with a 5 s join, and must be a
   refusal.
   - **Red on today's code:** the thread is still blocked. The test's `finally` opens the FIFO for
     writing, so nothing is left hanging.
4. **`test_a_link_to_a_workspace_folder_is_refused`:** a link to `sub`, nobody racing; a read
   through it and a listing of it are both refused. Nothing holds this today: the existing test
   uses an outside target.
5. **`test_a_path_through_a_file_gives_no_content`,** **`test_a_fifo_is_refused`** (POSIX) and
   **`test_an_unreadable_file_or_folder_is_refused_by_name`** (POSIX): the rows of §2.3 and one
   answer of today's that no test holds.
6. **`test_listing_lines_keep_their_format`:** a folder with a file, a folder and a folder link.
   The file's line is compared whole, and the other two by their kind and name. This holds the
   Windows listing to the POSIX format.
7. **`test_a_big_directory_is_counted_exactly`:** 400 files with 100-character names, one of them
   non-ASCII. The "more entries not shown" count must be exactly 200, and the non-ASCII name must
   list. On Windows that is about 107 KiB of directory records, more than one buffer.
8. **`test_an_empty_folder_and_the_other_case`:** an empty folder lists as "(empty directory)". A
   name in the other case reads on Windows and is `Not found` on Linux.

The existing tests stay green. `test_jail_rejects` changes the function it calls (§2.4), and
nothing else in them changes.

**Mutants** (each red in its named test, after a green control, sources restored byte for byte).
The POSIX ones run in WSL and the Windows ones on Windows.

| Where | Mutant | Red in |
|---|---|---|
| POSIX | the walk's open without `O_NOFOLLOW` (the `stat` check also off, or it hides this) | 1 |
| POSIX | the last open by the full name, not `dir_fd` | 1 |
| POSIX | the listing by name, not by descriptor | 1 (`sub`) |
| POSIX | the `stat` by full name, not `dir_fd` | 1 (the helper's count of relative calls) |
| POSIX | no `O_NONBLOCK` | 3 (nobody has it open) |
| POSIX | the type check after the switch to blocking and the read | 3 (held open, silent: the read hangs) |
| Windows | no `FILE_OPEN_REPARSE_POINT` | 1 (the second round: the other folder's entries appear) |
| Windows | no reparse check on the handle | 2 and 4 |
| Windows | a component opened by its full name with `CreateFileW` | 1 |
| Windows | the listing by name, not by handle | 1 (`sub`) |
| Windows | the listing stops after one buffer | 7 |
| Windows | case-sensitive opens | 8 |
| both | the walk skips the root and opens the joined path | 1 |

## 5. Decisions

### X1 (Victor, 2026-11-09: (1)) — Windows lists a directory through its handle
- **(1) Recommended: through the handle,** with `GetFileInformationByHandleEx`: one call in a
  loop, and a fixed record layout read with `struct`. Nothing is resolved twice, so a listing has
  the same guarantee as a read.
- **(2) By name, as today.** Smaller, but a swap between the walk and `os.scandir` lists an
  outside folder: names and sizes, no content. `verifier` ran it end to end: with a handle held
  on a folder, the folder itself was renamed away, a junction put in its place, and `os.scandir`
  by name listed the outside folder (§9, E). `SECURITY.md` would carry it.

### X2 (Victor, 2026-11-09: (1)) — The FIFO hang goes in with this step
It's the same check-then-open on the same lines. The fix is one flag, and the type check moving
behind the open. Leaving it out costs more than v1 said: stuck calls use up the thread pool that
runs share (§1).
- **(1) Recommended: in.**
- **(2) Out,** recorded as a known issue.

### X3 (Victor, 2026-11-09: (1)) — Windows: the same walk, or V4's handle check
V4's sentence was "on Windows, check that the opened handle's final path is inside the
workspace". v1 planned that and prototyped it. It stops the leak, and it leaves these, which
`skeptic` set out (finding 4):
- a racing guest can get **a different file from inside the workspace** returned;
- a racing guest can learn **whether a host path exists** (refused, or `Not found`);
- **the outside file is opened** before it's refused. Not read, but opening can have effects of
  its own (a cloud-only file may start downloading; not verified);
- a workspace on a drive that can't report a handle's path refuses every read, and a workspace at
  a drive's root needs its own rule in the comparison.

- **(1) Recommended: the walk of §2.2.** None of the five. `SECURITY.md` says one sentence for
  every platform. The cost is `NtOpenFile`, a native call (§2.2), where the handle check uses only
  Win32. Both prototypes are about the same size.
- **(2) The handle check, as V4 worded it.** Win32 only. `SECURITY.md` lists the first three as
  what Windows leaves. The by-name walk stays on Windows, because it is then the only thing that
  refuses a link to another workspace folder, and tests 1 and 2 assert less there.

## 6. Build order, each with its check

1. The tests of §4. **Check:** 1 and 3 are red on today's code for the reason stated, 2 is red on
   Windows, 4 is green (it holds an answer of today's that no test holds yet), and 5–8 are green
   except where §2.3 says an answer changes.
2. The POSIX path. **Check:** `test_workspace.py` green in WSL on 3.14 and 3.11.
3. The Windows path. **Check:** `test_workspace.py` green on Windows on 3.11, and on 3.12–3.15
   through the kit's `uv` (`fstat` and `ctypes` details differ by version, so each one is run).
4. The mutants of §4.
5. **Evidence, not tests** (a timed race as a test would only ever flake towards green):
   - the 4 s race of §1 against the built code, both platforms: 0 leaks expected, counts recorded;
   - **a real guest:** a native run in WSL, and a container run on Windows, each flipping a folder
     and a link to `../outside` in a loop while the host calls `read_workspace_entry`. Run on
     today's code first, then on the built code, with counts for both.
   - **On Windows the "before" count may be 0 and mean nothing:** a guest's link is one Windows
     follows only with Developer Mode on and a target that exists on the host. The run records
     the kind of link that landed on the host. If Windows wouldn't follow it, the record says the
     Windows guest run shows nothing either way, and §1's host-side numbers stand for Windows.
6. The gate on both platforms (it includes `test_acceptance.py`'s real-guest symlink check, the
   only one that reads a Windows listing made by a guest), then `critic`, then `verifier` on every
   finding.
7. The docs of §7. **Check:** `tests/test_docs.py`.
8. **Victor:** after this is pushed, run the manual macOS job **on this branch** (Actions → macOS
   check → Run workflow → pick the branch). It has 0 runs, and a run made earlier wouldn't cover
   these tests. §8's macOS sentence follows what it finds.

**Size:** `workspace.py` goes from 171 lines to about 280 (about 75 removed; POSIX about 45 and
Windows about 125 added, of which about 45 are declarations and constants). Tests about 220 more.

## 7. Files

| File | Change |
|---|---|
| `src/vestibule/workspace.py` | §2; the docstring's "Residual risk" paragraph rewritten to what holds |
| `tests/test_workspace.py` | §4 |
| `docs/plans/M1/M1-container-backend.md` | a dated amendment note on §6: the walk, the `commonpath` sentence, and the residual sentence |
| `docs/plans/M4/M4-step4-docs.md` | V4 marked done, with X3's outcome; §3's residual row takes §8's text |
| `docs/BUILD_LOG.md` | the entry; a known-issue row for the huge folder (§3) |
| `docs/HISTORY.md`, this plan | as usual |

**Not touched:** `server.py`, the tool descriptions, `README.md` (4a rewrites it), `CLAUDE.md`,
`docs/ARCHITECTURE.md` (it doesn't describe the jail's open; `skeptic` checked).

## 8. What `SECURITY.md` will say (4a writes it; this fixes the facts)

**Rewritten at the build** (§10): the first draft said "on every platform … and nothing else",
and the review showed where that stops.

- On a local drive, on every platform, `read_workspace` opens each part of a path once, from the
  folder it already holds open, and never follows a link. A guest changing the workspace during
  the call can make it fail, or get it a file's older content. It can't make the tool return, or
  ask about, anything outside the workspace.
- A file's content can change while it's read.
- **A Windows server whose workspace is on a network or WSL share** (`VESTIBULE_WORKSPACE` set to
  `\\wsl.localhost\…`): the far side looks every name up again, so the walk is by name there, as
  it was before this fix. No outside content came back in the tests, because Windows doesn't
  follow that share's links. A guest that swaps a file for a FIFO at the right moment can hold one
  call for about 10 s. Other kinds of share weren't tried.
- A directory listing shows a guest's file names. On a Linux or macOS host a name can hold a line
  break and so look like more entries, and a listing isn't cut to the output cap: 200 entries at
  most, which can come to about 150 KB.
- macOS runs the same code as Linux. Whether its tests ran there is stated from the manual job's
  result.

## 9. Review (`skeptic`, then `verifier`, 2026-11-09)

| # | Sev. | Finding | In v2 |
|---|---|---|---|
| 1 | med | The kept by-name `lstat` walk tells a racing caller whether a host path exists and what it is, on POSIX too. On Windows that walk is the only thing refusing a link to another workspace folder, and no test holds that | The by-name walk is gone (§2). The per-component `stat` is relative. §1 names the effect. Test 4 |
| 2 | med | Two mutants couldn't go red as written (a FIFO with no writer reads as empty; a link opened as itself reads as empty). The helper's guard was too weak. One mutant was killed only by a test needing a privilege | Test 3's second form; test 2 asserts a refusal; test 1 asserts the opens it saw, and its second round uses a junction |
| 3 | med | A workspace at a drive's root would refuse every read under v1's prefix rule; M1's "never string prefix" | No strings are compared under X3 (1). §3's row. The M1 note covers the sentence. Under X3 (2) the comparison strips the root's trailing `\` and is a pure function with its own test |
| 4 | med | The walk relative to a directory handle was dismissed on line count and not put to Victor | Prototyped and measured (§2.2). X3 |
| 5 | low | `SECURITY.md` would state the property for macOS with no run sequenced | §6 step 8; §8's third point |
| 6 | low | v1 specified the NT path form and measured the drive-letter form | Gone with X3 (1). Under (2) it's measured at the build |
| 7 | low | "Test 2 is red on today's code" is false on POSIX | §4, test 2 |
| 8 | low | The Windows real-guest run can read 0 before and after | §6 step 5 |
| 9 | low | "Today's answer and today's message" isn't true for unreadable entries | §2.3's table; test 5 |
| 10 | low | `O_NONBLOCK` changes the answer for a file under a lease | §3's row |
| 11 | low | The size was low; the docs step had no check; a Windows-only stand-in test would be a skip on Linux | §6's size and step 7. That test is gone with X3 (1) |
| 12 | low | A listing reads every entry before cutting to 200 | §3's row; a known-issue row. Not changed here |

`skeptic` on the decisions: X1 (1) holds; X2 (1), with the thread-pool cost added to §1.

**`verifier`** ran these on WSL (kernel 6.6, 3.14 and 3.11) and Windows (3.11 to 3.13), and
left nothing behind:

| | Claim | Verdict | What it adds or corrects |
|---|---|---|---|
| A | The by-name walk tells a racing caller about an outside path | CONFIRMED | The reliable signal is "exists and isn't a folder"; a folder looks like nothing there. Today's code has two more such messages. v1's "and nothing else" for POSIX was false. All of it goes with the by-name walk |
| B | A FIFO with no writer reads as empty at once; with a silent holder a blocking read hangs | CONFIRMED | The "type check after the read" mutant needs the held FIFO and a blocking read. Test 3's second form |
| C | On Windows a file link opened as itself reads as an empty regular file | CONFIRMED | Same on 3.11, 3.12 and 3.13. A junction opened as itself fails to read. So "no reparse check" returns `''`, and test 2 must assert a refusal |
| D | A lease stalls today's open for 45 s; the seccomp profile lets a guest take one | CONFIRMED | Run with a real native guest. The holder must handle `SIGIO`. §1 and §3 |
| E | A held handle stops a folder's ancestors being renamed, not the folder itself | CONFIRMED | Run end to end with a host-side rename, not a container guest's. X1 |
| F | Stuck `read_workspace` calls use up the thread pool that runs share | CONFIRMED | The run isn't refused: it loses its limits and says so. `native_cgroup.py:97`, `container.py:711`, `:684`, `:736`, `select.py:505` share the pool. §1 |
| G | An empty folder's listing ends with "no more files"; an empty drive root may say "file not found" | CONFIRMED / PLAUSIBLE | The folder half was run. The root half couldn't be: no empty drive, and no admin rights to make one. §3 |

## 10. As built (2026-11-09)

**Built as §2 says, with these changes:**

- **`_is_symlink_or_reparse` stays.** §2.4 was wrong: `backends/container.py` imports it for its
  run-dir sweep. Only `resolve_in_workspace` and its `commonpath` check are removed.
- **Windows opens each name for its attributes only, then reopens the last handle for reading**
  (review round 1, finding 3).
  - §2.2's single open asked for read access at once. With the workspace on a WSL share, that
    open waits about 10 s on a guest's FIFO and leaves a reader waiting in WSL, where the old code
    refused at once. Docker Desktop does mount such a path, so a guest can put a FIFO there.
  - Now a name is opened for attributes, a reparse point is refused, and the handle is reopened
    for reading by an empty name. The root is opened the same way.
  - On a local drive the reopen looks no name up, so the walk's guarantee is unchanged. On NTFS it
    buys nothing: a guest's FIFO there is a plain reparse file that opens at once.
- **No switch back to blocking on POSIX** (round 1, finding 5). A regular file's read doesn't
  wait on ext4, tmpfs or `/mnt/c`, so the line did nothing. The Windows regular-file check after
  the open went the same way: nothing but a regular file reaches it.
- **A name too long for Windows' 16-bit length field is `Not found`.** Without the guard,
  `ctypes` keeps only the low 16 bits of the length, and a 32,772-character name opened the
  4-character file `aaaa`. That stays inside the workspace, so it was a wrong answer and not an
  escape.
- **A listed name that isn't valid text shows the replacement character** (Victor, 2026-11-09).
  - `critic` found, and `verifier` ran with a real native guest, that a file named with invalid
    UTF-8 made the next listing stop the whole server. The name can't be written as JSON, and the
    SDK's writer fails after the handler has returned, so the handler's catch-all can't help.
  - It was in the code before this step. The file stays in the workspace and `.` is the default
    path, so a restarted server would have hit it again.
  - On Windows a name with a lone surrogate did the same, from a writer that isn't a container
    guest: through Docker Desktop a bad name already arrives with the replacement character.
- **The empty drive root's "file not found" (§3) is handled and was never run.** No test covers
  those two lines.
- **The case-insensitive flag can't be shown to matter here.** Windows forces case-insensitive
  lookups system-wide by default (`obcaseinsensitive` is 1). The flag stays, because it's what the
  call's contract asks for.
- **§8 is rewritten:** the guarantee is for a local drive, and it names what a share and a
  listing leave.

**Tests, where they differ from §4:**
- **Test 2 is red on the old code on POSIX too,** at the helper's own guard: the old code makes
  no relative opens. §4 said green.
- **The "stat by full name" mutant is red in a new test,** not in test 1 as §4's table said: the
  helper counts opens, not stats.
- **Four tests §4 didn't have:**
  - `test_a_swap_tells_nothing_about_the_other_folder`: plan-review finding 1 as a regression
    test. `sub/name/z` with `sub` swapped for a link gets the same answers whether the outside
    `name` is a file, a link, or absent. Red on the old code.
  - `test_a_name_too_long_to_exist_gives_no_content`.
  - `test_a_name_that_isnt_text_is_listed_readably`. Red on the old code.
  - `test_nothing_is_opened_for_reading_before_it_is_known_what_it_is`: every POSIX open is
    non-blocking, every named Windows open asks for attributes only, and a link as the last name
    is refused before anything is opened for reading.
- **The sweep asserts two more things at every call point:** each descriptor or handle that was
  opened was closed, and a guest's rename of the entry the tool holds open isn't refused.
- **Test 3's "Windows has none"** is true of NTFS. On a WSL share a guest's FIFO is a real one.

**Red first** (the old `workspace.py`, new tests):
- **WSL, 3.14 and 3.11:** the eight cases of test 1 and both forms of test 3 ("still blocked after
  5 s"), plus the tests that hold a changed answer.
- **Windows:** the eight cases of test 1, test 2, and the path through a file.
- **Failing lines:** "call point 3 of 4 gave 'MARKER-OF-THE-OTHER-FOLDER'" and the like.

**Mutants,** each red in its named test after a green control, sources compared byte for byte
afterwards:
- **POSIX: 13 of 13.**
- **Windows: 21 of 22.** The one that stays green is the case-sensitive open, above.
- Beyond §4's list: the four close-nothing mutants, no delete sharing, a name opened for reading
  at once, the reopen done by name, two orders of the reparse check, and names listed as they are.

**Evidence, not tests** (§6 step 5):

| Run | Old code | Built code |
|---|---|---|
| A real native guest flipping a folder and a link, WSL, 6 s per row (before round 1's two POSIX line changes) | 8,003 of 226,507 reads returned the outside file; 88 of 245,737 listings | 0 of 589,063 calls |
| A real container guest, Windows, the final code | 5 of 23,513 reads; 13 of 27,768 listings | 0 of 51,562 calls |
| The 4 s host-side race of §1, Windows | (§1) | 0 of 45,752 calls |
| The same, WSL, 3.14 and 3.11 (before the round-1 changes) | (§1) | 0 of 859,896 calls |
| A WSL-side flipper against a Windows read loop on a WSL share | 0 of 1,552 calls | 0 of 1,532 calls: this run shows nothing either way |
| An odd file name made by a real native guest, then a listing | the server stopped | the listing came back, and the next call was answered |

On Windows the guest's link landed as a real NTFS symlink (tag `0xa000000c`) that the host
follows, so the "old code" numbers there do mean something.

**Review rounds on the diff:**
- **Round 1:** `critic` found 7 (1 high that was already in the code, 1 medium, 5 low).
  `verifier` confirmed all 7 and corrected two details: Docker Desktop can mount a WSL-share
  path, and the `ctypes` argument lists don't fit one line each.
  - Fixed: the name that stops the server; no test for leaked descriptors; the 10 s wait on a
    share's FIFO; nothing holding delete sharing; two dead lines; the size, a little.
  - Kept: the empty-drive-root lines, untested, because `verifier` showed NTFS answers that way
    for a first query that matches nothing.
- **Round 2:** `critic` found 6 (1 medium, 5 low). `verifier` confirmed the 4 it was given and
  lowered the medium one to low: the old code waits the same 10 s in the same race, so it isn't
  new, and no outside content came back.
  - Fixed: the claim is scoped to a local drive (the module's docstring, §8, M1's note); the test
    that a narrower broken version got past, twice; one constant inlined; a comment that named the
    wrong filesystem.
  - Not changed, and recorded as known issues: the share's by-name walk; a file name with a line
    break looking like more listing entries on a POSIX host; a listing not cut to the output cap;
    every entry of a huge folder being read. All four were in the code before this step.
  - On a share two raw `OSError`s can leave `read_workspace_entry` during a race. The handler
    turns them into "Error reading workspace: …", so nothing raises to the SDK.

**Size:** `workspace.py` went from 171 lines to 351 (259 added, 79 removed) against "about 280".
The Windows half is the overrun: declarations and constants are about 70 lines, and the second
open added about 10. `tests/test_workspace.py` grew by 390 lines against "about 220": four more
tests and the helper's log of arguments and results.

**Gate:** green on the final code.
- **Linux (451 s of 540):** 3.14 full suite 1037 passed, 0 skipped; 3.11 Docker-free
  987; stdio 7.
- **Windows:** 845 passed, plus 172 POSIX/Linux-only skips, none of them for Docker.
- `tests/test_workspace.py` alone also passed on Windows 3.12, 3.13, 3.14 and 3.15.

**Left for Victor:** the manual macOS job on this branch after the push (§6 step 8).
