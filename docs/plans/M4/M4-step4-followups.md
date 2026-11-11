# M4 step 4 follow-ups — directory listings and the `docker`-group hint

Status: **v2, after the `skeptic` review (§11). Signed off by Victor 2026-11-10, with option (1)
on K1 and K2. Built 2026-11-10: §12 is what the diff review changed, §13 the as-built.**

Victor asked (2026-11-10) for the open issues that can be fixed to be fixed before step 5 is
planned. These are the two rows in `docs/BUILD_LOG.md`'s known issues marked "Open": the three
`read_workspace` listing issues (2026-11-09, race fix, critic round 2) and `setup`'s message for
a user outside the `docker` group (2026-11-09, step 4a). All four were there before M4. Nothing
is published here.

## 1. The problems, measured today (`5a1bc23` code, 2026-11-10)

**A. A file name can forge listing lines.** A listing is one line per entry, `kind size  name`,
and a name goes in as it is. A guest that makes a file named
`a.txt<line break>file              42  secrets.txt` gets this back from `read_workspace` on a
Linux host:

```
file               1  a.txt
file              42  secrets.txt
```

The second line is not an entry. The result goes to the model, so a prompt-injected run can show
the agent files that aren't there, or a fake "more entries" footer.

**B. A listing isn't cut to the output cap** (`max_output_bytes`, 20,000 by default). 200 entries
at most, but with long names:
- 200 names of 255 control bytes: 55,599 bytes back (Linux).
- 200 names of 255 bytes that aren't valid text: 156,399 bytes back (Linux).
- 200 names of 253 characters: 95,199 bytes back (Windows).

**C. Every entry of a folder is read, and its size asked for, before the cut to 200.**
- Linux (WSL, ext4), 1,000,000 empty files: 7.1 to 7.5 s for one listing, and 133 MB of Python
  objects.
- Windows (NTFS), 200,000 files: 3.1 to 3.3 s.
- **The size is the slow part on a slow disk.** With the workspace on WSL's `/mnt/c` (9p),
  10,000 entries with an `lstat` each took 4.1 to 4.3 s (five runs). Their names alone took
  0.015 s. On ext4 the same 10,000 with `lstat` took 0.015 to 0.027 s.
- The call runs in a worker thread, so the event loop isn't blocked, but that pool is shared
  (cgroup moves use it), and a guest can make the folder as large as the disk allows.

**D. A Linux user who isn't in the `docker` group is told to start Docker.** Docker 28.5.1 says
`permission denied while trying to connect to the Docker daemon socket at unix://…` (measured
with a socket this user may not open). Vestibule's refusal quotes that line and then says
"Start docker (Docker Desktop on Windows/macOS), then retry." The README's troubleshooting gives
the real fix and admits the message doesn't.

## 2. The listing (`src/vestibule/workspace.py`)

### 2.1 One entry is always one line (A)

- **Every name is escaped before it's put in a line.** A backslash is doubled. Each control
  character, format character, surrogate, and line or paragraph separator becomes a backslash
  escape with its code: a line break becomes `\x0a`, and a character above 0xFF gets the longer
  `\u` form.
- **The set of characters is the `audit` command's** (M3 R1: categories Cc, Cf, Cs, Zl, Zp).
  The function is the listing's own, a few lines in `workspace.py`, for two reasons:
  - **The server must not load `audit_cli`.** That module prints to stdout, and three texts
    say only the `audit` command imports it (`docs/ARCHITECTURE.md`, its own docstring, and
    `rootfs._say`'s comment). Golden rule 1 is why.
  - **The rule differs by one thing.** R1 leaves backslashes alone because code is full of
    `\n`. A file name isn't code. Doubled, a backslash in a listing always starts an escape,
    so a name of valid text can't imitate an escaped one (§12 D3 narrowed this sentence).
- **When a shown name was changed, a last line says so:**
  `...[names are shown with backslash escapes for control characters]`. The model reads the
  listing, not the README. The line appears only when it applies, so a listing of ordinary
  names is byte for byte what it is today.
- **`read_workspace` can't open an escaped name by the text shown,** and can't open a name
  with a backslash at all (a backslash in a path is a separator, as today). `run_code` can.
- **Not changed:** a file's *content* is returned as it is. A file is whatever its bytes say,
  and its truncation marker can be imitated by content, as a run's stdout can.

### 2.2 The listing fits the output cap (B)

- Lines are added in name order while the text stays within the cap, and still at most 200.
- **The cap is counted in characters,** as a run's output is (`server._truncate`). A file's
  cap is counted in bytes before decoding, so neither is "bytes of the reply"; characters is
  the simpler of the two and the one the model's context is measured closest to.
- The footer counts every entry not shown, for either reason:
  `...[N more entries not shown]`, today's wording.
- The footers come after the cap, as a file's truncation marker does. They are short and of
  fixed shape.
- **Edge:** if `VESTIBULE_MAX_OUTPUT_BYTES` is so small that the first line doesn't fit, the
  listing is the footer alone.

### 2.3 A folder is read up to a cap, and sizes are asked only for what is shown (C)

- `_MAX_SCAN = 10_000`. Each lister stops after that many entries and reports whether there
  were more. On Windows the `.` and `..` records don't count.
- **On POSIX the scan reads names only.** The kind and size of an entry come from one `lstat`
  by name, relative to the folder's descriptor, never following a link, and only for an entry
  that is about to be shown: at most 200 per listing. An entry that vanished in between is
  left out. (§12 D1 and D4 say how the 200 is kept, and what a lookup that fails otherwise
  shows.)
- **On Windows nothing changes but the cap:** each record already carries the kind and size.
- Up to 10,000 entries the listing is what it is today: sorted by name, the first 200 shown,
  the rest counted.
- Past it, the footer says what the listing is:
  `...[N+ more entries not shown: the folder holds over 10000, and only the first 10000 read were sorted]`.
  Which 10,000 is the filesystem's order, not name order.
- **Rejected: keep the best 200 names while reading everything.** It bounds memory but not
  time, and the guest picks the number of files.
- **v1 kept the `lstat` per entry read,** on the ext4 number. On 9p that is over 4 s per
  listing for a guest that made 10,000 files, and a home directory on NFS or FUSE may be the
  same.

## 3. The `docker`-group hint (`src/vestibule/backends/select.py`)

- A helper beside `_snap_hint`, used in `_resolve_runtime`. When the runtime is `docker`, the
  host is Linux, and its status holds Docker's own phrase
  `permission denied while trying to connect` (any case), that runtime gets this in place of
  "Start docker …, then retry.":

  > This user may not open docker's socket. Add the user to the `docker` group
  > (`sudo usermod -aG docker $USER`), log out and back in, then restart your agent. Members of
  > that group can become root on this machine.

- **The phrase, not the two words.** `_version_check` keeps the first line of Docker's stderr,
  cut to 200 characters. An unreadable `~/.docker/config.json` makes that first line a
  `WARNING: … permission denied` about the config file, with the real cause after it. Matching
  "permission denied" alone would give that user the group hint. The phrase is Docker's for
  the socket, and it is at the start of the line, so the cut can't lose it.
- **It doesn't say Docker is running.** A socket file left behind with no daemon gives the
  same line.
- **It stays an ordinary refusal** (`RunRefusedError`), re-checked after 30 s like today's.
  - v1 made it restart-only. That would stop the re-check that moves a session from
    `native-degraded` to a container once the runtime works (`_select_auto_linux`), and a
    restart-only message may not hold another runtime's "then retry".
  - The cost: the cached copy says when the next check is, beside "restart your agent". Both
    are true.
- **Only `docker`, only Linux.** Podman can say "permission denied" for other reasons, and
  `usermod` isn't macOS's. Any other runtime that is down keeps its own "Start …, then retry."
  in the same message.
- **The last sentence** is `SECURITY.md` §3.11 in eleven words. The message tells someone to
  join that group, so it says what the group is.

## 4. Tests (both platforms; none skipped on Linux)

In `tests/test_workspace.py`:
1. **A line break in a name can't forge an entry** (POSIX; NTFS can't hold one). The listing has
   exactly one line per entry plus the note, and the forged text appears only inside an escaped
   name. Red today.
2. **Escapes are exact** (both platforms: a right-to-left override and a line separator are
   legal in NTFS names): each becomes its escape, a backslash is doubled where the filesystem
   allows one in a name, and a listing of ordinary names has no note and is unchanged.
3. **A listing fits the cap:** names of mixed lengths with a small cap. The lines shown are
   whole, are exactly the first k in name order, fit the cap, and shown plus "not shown"
   equals the total. Red today.
4. **A folder is read up to the cap, and no further.** With `_MAX_SCAN` lowered in the test:
   - a folder past it gets the "over" footer, and one exactly at it doesn't (on Windows too,
     where `.` and `..` must not count);
   - **the number of entries read is counted,** which the race fix's stand-in can't do as it
     is: on POSIX a stand-in `scandir` returns an iterator that counts what is taken from it.
     On Windows one call returns a buffer of many records (about 740 short names), so the
     folder's names are long enough to span several buffers and the calls are counted. A stop
     that is one entry late shows there only through the footer, which the bullet above pins.
   Red today.
5. **Sizes are asked only for entries shown** (POSIX): the stand-in counts `stat` calls, and a
   folder of 1,000 names costs at most 200 of them, plus the walk's. Red today.
6. `test_a_big_directory_is_counted_exactly` keeps its meaning: its names are shortened so that
   the 200-entry rule, not the cap, is what it tests.

In `tests/test_select.py`:
7. **Docker's socket line gets the group hint** and no "Start docker", and is still an ordinary
   refusal. Each of these gets today's message: the same line on macOS or Windows; the same
   line from `podman`; a daemon that is simply down; and a first line that is the config-file
   `WARNING` with "permission denied" in it. With `podman` also down, its own "Start podman …"
   is still there. Red today.

**Proving they can fail,** each red in its named test after a green control:
- the escape removed; backslashes not doubled; the note always on, and never on;
- the cap removed; a long line skipped and shorter ones still added; the count of entries not
  shown off by the ones the cap dropped;
- the scan cap removed; off by one; **`continue` in place of `break`** (the right listing, and
  every entry still read); the "over" flag dropped, on each platform; `.` and `..` counted;
- an `lstat` for every entry read;
- the hint's Linux, runtime and phrase conditions each removed; the phrase loosened to
  "permission denied".

## 5. Checked on a real machine (main session, after the gate)

- **Linux, native:** through the server over stdio, a real guest makes a file with a line
  break in its name and a folder of 20,000 files. `read_workspace` lists the first as one
  line with the note. The second gets the "over" footer within 0.1 s.
- **Linux, workspace on `/mnt/c`:** the same 20,000-file folder listed within 0.5 s. Today
  that takes over 4 s.
- **Windows, container:** the same guest code. A line break in a guest's file name is expected
  to arrive as a private-use character, which is one line and not escaped, so the pass is
  "one line per entry", with or without the note. The big folder gets the "over" footer within
  0.1 s.
- **The hint:** `vestibule-mcp setup` in WSL with `VESTIBULE_BACKEND=container` and
  `DOCKER_HOST` naming a socket this user may not open, which is the real Docker CLI's real
  refusal. It prints the hint and exits non-zero.

## 6. What can go wrong

| Case | Handling |
|---|---|
| A model reads an escape in a name and passes it back as a path | `Not found`, or the refusal for a backslash, as today for any wrong name. The note says what the escapes are. |
| A folder of 10,001 files, sorted listing expected | The footer says the listing covers the first 10,000 read. Below that, nothing changes. |
| A guest keeps creating and deleting files during a listing | The cap is on entries read, so the call still ends. An entry gone before its `lstat` is left out. |
| A guest swaps an entry for a link between the scan and its `lstat` | The `lstat` doesn't follow it, and the line says `symlink`. Nothing is opened. |
| A filesystem that gives no entry types | Not relied on: the kind comes from the `lstat`. |
| `max_output_bytes` far above a listing's size | Unchanged: 200 entries. |
| Docker's wording changes | The hint is missed and today's message is shown, which the README's troubleshooting still covers. |
| A socket left behind with no daemon | The hint is shown, and is wrong about the cause. It doesn't claim Docker is running, and the refusal is re-checked, so starting Docker still works. |
| A user who can't be put in the `docker` group (shared machine) | The message's last sentence, and the README's rootless note. |

## 7. Docs

- **`SECURITY.md` §3.12:** the listing bullet says what is left: a folder past 10,000 entries
  is listed from the first 10,000 read, and a file's content is returned as it is.
- **`README.md`'s troubleshooting:** the `daemon not reachable` line loses "(The message itself
  only says to start Docker.)".
- **`docs/plans/M1/M1-container-backend.md` §6:** a dated amendment to "capped at 200 entries".
- **`docs/BUILD_LOG.md`:** both rows closed, a status row and an entry. **`docs/HISTORY.md`:** a
  bullet.
- Check: `tests/test_docs.py` and `tests/test_pins.py` on both platforms, and the README render.

## 8. Decisions

### K1 (Victor, 2026-11-10: (1)) — How many entries a listing reads
- **(1) Recommended: 10,000.** Names only: 0.015 s even on the slowest disk measured (9p).
  Every folder a person or an agent would browse behaves as today.
- **(2) 200,** the number shown. Simplest, but a folder of 300 files would stop being sorted by
  name and counted exactly.

### K2 (Victor, 2026-11-10: (1)) — The hint's last sentence ("Members of that group can become root on this machine.")
- **(1) Recommended: keep it.** The message tells a user to do something with a security cost
  that `SECURITY.md` already states.
- **(2) Drop it.** Shorter, and the README says it.

## 9. Build order, each with its check

1. Tests 1 to 5 and 7, red for the stated reason; test 6's names changed and still green.
2. The escape, the note and the cap. Check: tests 1 to 3 green.
3. `_MAX_SCAN` in both listers, and POSIX's names-only scan. Check: tests 4 and 5 green on
   both platforms.
4. The hint. Check: test 7 green.
5. The mutants of §4.
6. The gate on both platforms.
7. `critic` on the diff, then `verifier` on every finding.
8. §5's live checks, then the docs of §7 with their check.

**Size:** `workspace.py` about 45 lines, `select.py` about 15, tests about 220. The last step on
this module planned 220 lines of tests and wrote 390.

## 10. Files

| File | Change |
|---|---|
| `src/vestibule/workspace.py` | §2 |
| `src/vestibule/backends/select.py` | §3 |
| `tests/test_workspace.py`, `tests/test_select.py` | §4 |
| `SECURITY.md`, `README.md`, `docs/plans/M1/M1-container-backend.md`, `docs/BUILD_LOG.md`, `docs/HISTORY.md` | §7 |

**Not touched:** `server.py` (it already passes `max_output_bytes`, and the tool description
stays as it is), `audit_cli.py`, `setup_cli.py` (the refusal's class doesn't change, so its
branches and comments stay right), the container preflight's own "start it" message, the naive
backend's leftover temp dir (dev-only; its row stays).

**Outside this step, asked of Victor (2026-11-10: fix both):** `CLAUDE.md`'s "Security posture"
paragraph still says "structurally impossible", which step 4a took out of the README; and
`docs/HISTORY.md`'s M2 step 2 bullet still says "review fixes pending" (they are `4c8afcf`). The
main session makes both edits. They are docs only and aren't the builder's.

## 12. The diff review, and what it changes in §2 and §4 (2026-11-10)

`critic` reviewed the built diff: 2 medium and 2 low, no golden rule broken. `verifier` confirmed
all four. The fixes below are part of this plan, and where they differ from §2 they replace it.

| # | Sev | Finding (confirmed) | Fix |
|---|---|---|---|
| D1 | med | §2.3's "at most 200 `lstat`s" didn't hold. An entry whose `lstat` failed was skipped without counting toward the 200, so a guest that deletes its files between the scan and the listing, or makes its folder unsearchable, had every entry read looked up: 10,000 calls, 1.2 to 1.3 s on 9p | D1 below |
| D2 | med | No test reached "an entry gone since the scan". Three broken versions passed the whole file | Test 8 and its mutants |
| D3 | low | "No two different names look alike" is false for a name that isn't valid text: both listers decode such bytes to the replacement character before escaping, so `a` + 0xFF and `a` + 0xFE list alike. For the same reason no surrogate reaches the escape | D3 below |
| D4 | low | Any failed `lstat` counted as "gone", so a folder the guest made readable but not searchable (`chmod 444`) listed as `(empty directory)`. HEAD does the same | D4 below |

- **D1. Only the first 200 names are looked up.** `_render` asks for the kind and size of the
  first 200 entries in name order and of no other. An entry that is gone leaves its place
  empty: no later entry is looked up in its stead, and the footer counts the rest as not shown.
  So a listing costs at most 200 `lstat`s on POSIX whatever the guest does meanwhile.
- **D3. The claim is narrowed, the behaviour isn't changed.** A backslash in a listing always
  starts an escape, so a name of valid text can't imitate an escaped one. Names that aren't
  valid text keep showing the replacement character, as since the race fix, and two of them
  can look alike. §2.1's sentence "no two different names look alike" and the same words in
  the code's docstring go. The surrogate category stays in the set: it is the `audit`
  command's set, and it is what keeps a listing valid if a lister's decoding ever changes. A
  comment says no surrogate reaches it today.
  - Not taken: showing each invalid byte as its own escape. It would make the listing exact,
    and it would change what the race fix chose for such names and its test.
- **D4. Only a vanished entry is left out.** If the `lstat` fails for any other reason, the
  entry is shown with the kind `unknown` and `?` for its size. `(empty directory)` is then
  only said of a folder that has no entry left.

**Tests added to §4:**

8. **Entries gone since the scan** (POSIX): a stand-in removes some entries after the scan.
   The lines left are the right ones, the footer's count is right, and the `lstat`s are at
   most 200 even when every one of 1,000 entries is gone.
9. **An entry that can't be looked up is shown, not hidden** (POSIX, not as root): a folder
   made readable but not searchable lists its names as `unknown`, with at most 200 lookups.

**Mutants added:** the `try`/`except` around the `lstat` removed; `continue` made `break` for a
gone entry; the gone entries left out of the footer's count; a gone entry's place refilled (the
code as first built); every failure treated as gone again.

**`verifier`'s corrections to the review's numbers:** 10,000 lookups, not 10,001 (the entry read
past the cap is never looked up); and past the scan cap the old result was the "1+ more" footer,
not `(empty directory)`.

## 13. As built (2026-11-10)

**Built as §2, §3 and §12 say.** `builder` wrote it in two rounds: the plan, then §12's fixes.

**Where the build read the plan, or went past it:**
- **The "over" footer's number** counts every entry read and not shown, including the one read
  past the cap to learn there were more: `9801+` for a folder over 10,000.
- **A character above U+FFFF** gets the eight-digit `\U` form, as the `audit` command's escape
  does. §2.1 named only the shorter one.
- **The note appears for any change to a shown name,** a doubled backslash included.
- **Any `lstat` failure other than "not found" shows the entry as `unknown`** with `?` for its
  size (§12 D4). The signed plan had no such line. It replaces a false `(empty directory)`.
- **If every entry of a folder is gone by the time it is looked up,** the listing is
  `(empty directory)`, or the footer alone when the folder held more than 200.
- **Tests are wider than §4's list:** a control character from the C1 range, a name that reads
  like an escape, an older Docker's wording of its line, and that the refusal is served from
  the cache with its re-check note.
- **Size:** `workspace.py` grew by 73 lines against "about 45", `select.py` by 29 against "about
  15", and the tests by about 260 against "about 220". The overrun in `workspace.py` is the
  loop that keeps the cap, the count and the note together, and the hand-off that lets each
  platform say how an entry is looked up.

**§5's checks, on the final code:**
- **Linux, native, ext4:** the forged name is one line with the note. 20,000 files: the "over"
  footer in 0.010 s.
- **Linux, workspace on `/mnt/c`:** the same folder in 0.108 s.
- **Windows, container:** the line break arrives as a private-use character: one line, no
  note. 10,500 files: the "over" footer in 0.187 s. **That misses §5's "within 0.1 s".** The
  number was set too low for Windows, where parsing 10,000 records takes most of that; the old
  code took 3.1 s for 200,000 files.
- **The hint:** `setup` against a socket this user may not open printed it, exited 1, and
  wrote nothing to stdout.

**Mutants:** round 1, 21 of 21 on both WSL Pythons and 18 of 19 on Windows (the one that
removes the doubling of backslashes can't fail there: an NTFS name can't hold one). Round 2,
6 new and 10 re-run: 16 of 16 on WSL, 4 of 4 on Windows.

**Review of the diff:** §12. A second pass by `critic` on the fixes found all four closed and
nothing new.

**Gate, on the final code:** Linux (409 s of 540): 3.14 full suite 1059 passed, 0 skipped; 3.11
Docker-free 1009; stdio 7. Windows: 863 passed, plus 176 POSIX/Linux-only skips, none of them
for Docker. `tests/test_docs.py` and `tests/test_pins.py` were run again after the docs.

**Outside the plan, with Victor's OK:** `CLAUDE.md`'s "Security posture" sentence, and the M2
step 2 line in `docs/HISTORY.md`.

## 11. Review (`skeptic`, 2026-11-10; `verifier` on its checkable claims)

`skeptic`'s verdict on v1 was "sound with amendments": 4 medium and 6 low. `verifier` checked
its nine claims about the code, the docs and this machine: none refuted, six confirmed and
three partly, with these corrections, which are in the text above:
- the 9p figure is 4.1 to 4.3 s, and ext4's 0.015 to 0.027 s;
- on Windows a lister that stops inside the first buffer makes one call fewer, so a missing
  cap is visible there by calls, and a stop one entry late is not;
- with the default socket path Docker's line is 215 characters, and the 200-character cut
  loses only its last 15, so the phrase at its start is always kept.

| # | Sev | Finding | In v2 |
|---|---|---|---|
| 1 | med | Test 4 couldn't see how many entries were read: the stand-in logs one `scandir` call, and one Windows call fills a buffer of hundreds of records. A `continue` in place of `break` would pass | Test 4 counts entries read on both platforms; the mutant is listed |
| 2 | med | The `lstat` per entry was kept on an ext4 number. On 9p, 10,000 of them take 4.3 s; and only the POSIX lister would change | §2.3: names only, `lstat` for what is shown; test 5; §5's `/mnt/c` check |
| 3 | med | "permission denied" alone misfires on an unreadable Docker config and on a leftover socket, and restart-only would stop the degraded re-check | §3: Docker's own phrase; no claim that Docker runs; an ordinary refusal |
| 4 | med | Importing `audit_cli` into the server breaks a boundary three texts state, and R1's reason for not doubling backslashes doesn't hold for file names | §2.1: the listing's own escape, backslashes doubled |
| 5 | low | A restart-only refusal carrying another runtime's "then retry"; `setup_cli`'s branches | Gone with finding 3's change |
| 6 | low | "UTF-8 bytes, as a file's cap is counted" was wrong twice; test 3 didn't pin the order | §2.2: characters; test 3 asserts the first k in name order |
| 7 | low | On Windows `.` and `..` would count toward the cap | §2.3, test 4 |
| 8 | low | The note said "\x escapes" for escapes that aren't all that form; a raw control character had got into this file | §2.1's wording; the file is plain text again |
| 9 | low | `CLAUDE.md`'s wording and the stale HISTORY line don't belong in this step | Moved out (§10) |
| 10 | low | Live checks without a pass criterion; 300,000 files through a bind mount is doubtful; the docs step had no check; the test size was light | §5, §7, §9 |
