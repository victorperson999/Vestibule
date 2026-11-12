# M4 step 5c — 0.1.1: a shorter page on PyPI

Status: **v2, 2026-11-11, after the `skeptic` review (§10). Signed off by Victor 2026-11-11,
with option (1) on Y2, Y3 and Y4: the Alpha label stays, `How it works (design)` stays on the
PyPI page, and the two "What stays" lines go in now. So the script takes out the badge, the
warning, and the sections `Status` and `Documentation`. Built, reviewed and released
2026-11-11: §11 is the diff review, §12 the as-built.**

Victor looked at the PyPI page of 0.1.0 on 2026-11-11 and asked for two changes to it:
- the page shouldn't carry the parts about how the project was built;
- the early-development warning should go.

GitHub's README stays as it is. His answers the same day (asked one by one):
- **What the page leaves out:** the sections `How it works (design)`, `Status` and
  `Documentation`. (`How it works` is asked again as Y3: the question described that section
  wrongly.)
- **The warning:** removed on PyPI only. GitHub keeps it.
- **When:** 0.1.1 first, then "M4 accepted".

## 1. Two facts this rests on

- **A published description can't be edited.** 0.1.0's stays as it is. The project's page
  shows the newest release's description, so a 0.1.1 with a shorter description is what
  changes the page. 0.1.0 stays listed under the release history, with its old text. It isn't
  yanked: nothing is wrong with its code.
- **The package code doesn't change.** 0.1.1 differs from 0.1.0 in its description and its
  version number, nothing else.

## 2. The approach

**One source, and the PyPI page is cut from it.** `README.md` stays the only text anyone
edits. A new file `PYPI.md` at the repo root is `README.md` with a fixed list of parts taken
out, and `pyproject.toml` names `PYPI.md` as the package's description.

- **Nothing is added to `README.md` for this.** v1 put marks into it; a mark next to the
  badge would have split GitHub's row of badges into three rows (§10, finding 1).
- **A small script, `scripts/pypi_readme.py`,** reads `README.md` and takes out the parts
  named in §3. Each part must be found exactly once, or the script stops with an error and
  writes nothing. It reads and writes UTF-8 with `\n` line endings.
- **`PYPI.md` is committed,** so anyone can read exactly what PyPI will show, and a doc test
  fails when it and the README drift apart. It is also needed there: an editable install (the
  gates' venvs, the macOS job) fails if the file `pyproject.toml` names is missing.

**Checked in a scratch copy of `HEAD` (2026-11-11), not assumed:** with `readme = "PYPI.md"`
the build passes, the wheel's description has `Quick start` and none of the removed parts,
`twine check` (PyPI's own renderer) passes, and the sdist holds 28 files: the 27 of 0.1.0
plus `PYPI.md`. The build tool puts the description's file into the sdist even when the
include list doesn't name it; the list will name it anyway, so the list stays the whole
truth.

**Not chosen:**
- **A build plugin that cuts the README at build time** (`hatch-fancy-pypi-readme`). It needs
  no second file, but it is one more package every build from source must download and
  trust, and what PyPI shows would exist only inside a built file.
- **A second file written by hand.** It drifts from the README the first time one is edited.
- **Cutting the README itself.** Victor wants GitHub's kept.

## 3. What the PyPI page loses, and what it keeps

**Taken out by the script:**
1. the "Status: Early Development" badge in the top row. It says what the warning says, so
   it goes with the warning Victor asked to remove;
2. the warning block under "What it is" ("Early development — do not rely on this for hostile
   code yet");
3. the sections `Status` and `Documentation`;
4. the section `How it works (design)`, or not: Y3.

**Kept:** `What is and isn't sandboxed`; `What it protects, and what it doesn't`, whose first
point is "Not a VM", with its link to `SECURITY.md`; and everything a user acts on (the quick
start per platform, the clients, troubleshooting, uninstall, `setup`, configuration, native
isolation, usage numbers and the audit log, the license).

**What `How it works` holds that nothing else on the page says** (§10, finding 2). It is two
paragraphs, and it doesn't describe how the project was built:
- what each `isolation:` value means, `native-degraded`, `container-degraded` and `none`
  included, and that a container run's protection is "handed to the container runtime to
  apply", with the pointer to what can't be checked on your machine;
- the limit on the whole promise: "out of the run's reach, short of a bug in the kernel or
  the container runtime".

Kept text leans on both: "honest reporting of what protection was actually applied" near the
top, and "the strongest guarantee that verifies" under native isolation. Without the section
the page says those things with no statement of their limits (golden rule 5).

**Links:** every link in the README is already a full `https://` address, so the ones that
stay work on PyPI as they do now. The only links to `#status` are the badge's and the
warning's, and both go.

## 4. Files

| File | Change | Who |
|---|---|---|
| `README.md` | None for the page. Two lines under "What stays" if Y4 is taken; text moved if Y3 is (2) | main session |
| `PYPI.md` | New, written by the script | the script |
| `scripts/pypi_readme.py` | New, about 40 lines; `--check` writes nothing and fails if `PYPI.md` differs | `builder` |
| `pyproject.toml` | `readme = "PYPI.md"`; `version = "0.1.1"`; `/PYPI.md` in the sdist's include list | `builder` |
| `src/vestibule/__init__.py` | `__version__ = "0.1.1"` | `builder` |
| `scripts/release_smoke.py` | `PYPI.md` joins the files that must be committed and the files the sdist must hold | `builder` |
| `tests/test_docs.py` | Tests 1 to 4 of §5 | `builder` |
| `tests/test_release_smoke.py` | Test 5, and the existing fixtures that list an sdist's files (`GOOD_SDIST`, the `stray` and `gone` cases, the pinned last five paths of the `git status` call) gain `PYPI.md` | `builder` |
| `docs/RELEASE.md` | `PYPI.md` among the shipped files (the list at the top and step 10's comparison); "the README is PyPI's description" becomes "`PYPI.md` is"; after a README edit, run the script; step 10's name check and step 12's version written for a release that isn't the first | main session |
| `docs/plans/M4/M4-step5-release.md` | A dated amendment to §2: `PYPI.md` is a shipped path | main session |
| `docs/plans/M4/M4-adoption.md` | A dated amendment to F1 (it fixes "version 0.1.0" and "the README's early development … stays"); the acceptance record says which version each criterion's evidence ran on | main session |
| `docs/BUILD_LOG.md`, `docs/HISTORY.md` | The record | main session |
| `m4spike\release\publish.ps1` (outside the repo) | 0.1.1's folder, files and hashes; its check becomes "version 0.1.1 isn't on PyPI yet" | main session |

The README's M4 row keeps "first release, 0.1.0": it is still true. `build` doesn't run the
script's `--check`: test 1 is in both gates, and the gates run on the candidate before the
build.

## 5. Tests (each shown to fail first, with a mutant)

1. `PYPI.md` is what the script makes from `README.md`, compared as text so a Windows
   checkout's line endings don't matter. Mutant: a word changed in `PYPI.md`.
2. **Nothing else is lost.** The headings of `PYPI.md` are the README's minus exactly the
   sections §3 names, and outside those sections the only lines missing are the warning and
   the badge. `PYPI.md` has no "Early development" in either spelling. Mutant: the script's
   section cut moved to end one section later (it would swallow `License`).
3. The script stops, and writes nothing, when one of its parts is found zero times or twice.
   Mutants: each of those checks removed.
4. `pyproject.toml` names `PYPI.md` as the description and lists it for the sdist. Mutant:
   `readme` set back.
5. `release_smoke.py build` fails on an sdist without `PYPI.md`.

## 6. What is run again, and what isn't

A shipped file changes, so this is a new candidate (step 5's §7): a new commit, one build,
new hashes, the two files put aside in `m4spike\release\0.1.1\`.

**Run again:**
- both gates on the candidate commit;
- `critic` on the diff, with its standing check that the README and `SECURITY.md` agree;
- `build`, with the sdist's file list (28 files) and `twine check`;
- `smoke` on Python 3.11 to 3.15, on Linux and on Windows;
- the secrets scan and the macOS job (Victor, one command and one click);
- every link in `PYPI.md` fetched;
- `PYPI.md` rendered with PyPI's renderer and its headings listed, so Victor sees the page's
  outline before the upload;
- after `main` has the commit: the shipped files on `main` equal the candidate's, the hashes,
  and that version 0.1.1 isn't on PyPI yet;
- after the upload: PyPI's hashes, `smoke` from PyPI on `vestibule-mcp==0.1.1`, and Victor's
  look at the page.

**Not run again:** the four first runs on the VMs, the uninstall tests and Cursor. No command
in any platform's section, nothing in `setup` and nothing in either backend changes; Y4 only
adds to the "What stays" list two things the uninstall test already saw. `smoke` installs the
new wheel and runs it on ten Pythons.

**Not in 0.1.1:** the finding that Claude Code doesn't show the `isolation:` line. Fixing it
means changing the sentence the first runs were made with (S5-D3).

## 7. What Victor does

1. Sign this plan, with Y2, Y3 and Y4.
2. After the build and the gates: commit, push, the secrets scan, the macOS job, the pull
   request to `main`.
3. The upload, with the helper, once it names 0.1.1's two files.
4. Look at the page.

## 8. What can go wrong

| Case | Handling |
|---|---|
| The README is edited later and `PYPI.md` isn't regenerated | Test 1 fails in the gate |
| A later edit renames a section or the warning | The script stops: a part isn't found once |
| A new section is added after `Documentation` | Test 2 fails until §3's list and the test say whether PyPI gets it |
| A later edit adds a link from kept text into a section PyPI leaves out | It opens GitHub's README, where the section is. Accepted |
| The page still shows the old text after the upload | PyPI's page is cached for a few minutes; the JSON record is checked first |
| 0.1.1 turns out broken | Yank 0.1.1. An install with no version then takes 0.1.0 again, and the project's page goes back to 0.1.0's long text |

## 9. Decisions

Decided by Victor's request, not asked again: the badge goes with the warning (§3).

### Y2 (Victor, 2026-11-11: (1)) — PyPI's own "Development Status" line
The package's metadata says `Development Status :: 3 - Alpha`. PyPI shows it in the page's
sidebar, outside the description.
- **(1) Recommended: keep it.** It is a small standard label, and it is true: the README's
  client table still has untested entries and macOS has never run a sandbox. It would be the
  one place the PyPI page still says the project is young.
- **(2) Change it to `4 - Beta`.**
- **(3) Remove the line.**

### Y3 (Victor, 2026-11-11: (1)) — `How it works (design)` on the PyPI page
Asked again because the first question called it a section "which points at the architecture
doc". It doesn't. §3 says what it holds.
- **(1) Recommended: keep it on PyPI.** Two paragraphs, and they are where the page says what
  the `isolation:` values mean and where the promise ends. `README.md` isn't touched.
- **(2) Leave the section out, and move its two limit statements** into `What it protects,
  and what it doesn't`, in the README itself. GitHub's README then changes too: the same
  words, in another section.
- **(3) Leave it out and move nothing.** The page keeps "Not a VM" and the link to
  `SECURITY.md`, and loses the rest.

### Y4 (Victor, 2026-11-11: (1)) — The README's "What stays" list, in 0.1.1
The uninstall test found two leftovers the list doesn't name, and Victor's answer was "fix
the wording in the next release". 0.1.1 is the next release, and its page can't be changed
afterwards either.
- **(1) Recommended: add them now.** Two lines under "What stays": uv's package cache
  (`uv cache clean` empties it), and your MCP client's own logs of the server. On GitHub and
  on PyPI.
- **(2) Not in 0.1.1.** The known-issue row stays open.

## 10. Review (`skeptic`, 2026-11-11: "sound with amendments")

One high, four medium, five low. The main session checked findings 1 to 4 against the files
itself (`README.md:7`, `:328`, `:330`; F1 in `M4-adoption.md`; the two BUILD_LOG rows); no
`verifier` run.

| # | Sev | Finding | Taken as |
|---|---|---|---|
| 1 | high | A mark beside the badge splits GitHub's badge row: a comment line starts an HTML block and ends the paragraph | No marks. The script cuts named parts, each found exactly once (§2). `README.md` isn't edited for the page |
| 2 | med | "The 'Not a VM' point says the same" was wrong: `How it works` alone defines the `-degraded` values and limits the promise | §3 rewritten; Y3 asked again with what the section holds |
| 3 | med | The tests couldn't see a cut that swallows kept text (`License`, or the sandbox-only section) | Test 2 compares headings and the lines removed |
| 4 | med | F1 in the contract fixes "0.1.0" and "the early development warning stays" | A dated F1 amendment; the acceptance record names the version behind each criterion (§4) |
| 5 | med | Silent on "fix the wording in the next release", and on two known-issue rows | Y4; §6 says what isn't in 0.1.1 and adds `critic`'s comparison |
| 6 | low | Existing `test_release_smoke.py` fixtures break when the sdist must hold `PYPI.md` | Named in §4 |
| 7 | low | The upload helper and several `RELEASE.md` lines only fit a first release | §4 |
| 8 | low | Encoding and line endings unspecified | §2, test 1 |
| 9 | low | The link tests on `PYPI.md` and the code-block refusal add nothing | Dropped |
| 10 | low | Yanking 0.1.1 brings back 0.1.0's page | §8 |

## 11. The diff review (`critic`, 2026-11-11)

No high finding. Two medium and six low; the main session checked the two medium ones by
reading the script, the test and `RELEASE.md`; no `verifier` run. What the review found
sound: the script cuts exactly the four parts from the real README; nothing is left
half-written; line endings; the page doesn't overclaim with the warning gone; the README and
`SECURITY.md` agree where they state the same fact.

| # | Sev | Finding | Fix |
|---|---|---|---|
| 1 | med | No test would notice the removal of the guard "the two sections are followed by `License`": the other four guards stop every case tried. Without it, a renamed `License` heading leaves `Status` and `Documentation` on the page | `builder`: a case for it, with that guard's removal as its mutant |
| 2 | med | `RELEASE.md` listed the VM first runs with no rule for skipping them, and step 3's reason was only true for a first release | Main session: step 9 says when it is skipped and that the record names the release it rests on; step 3 reworded |
| 3 | low | `--check` had no test | `builder`: one test through `main()` |
| 4 | low | Test 2 failed on a sub-heading added inside `Status`, which the script cuts correctly | `builder`: headings compared outside the left-out sections |
| 5 | low | The README's speed note said "the code from before M3"; the page has no milestone table | Main session: "from before the audit log was added" |
| 6 | low | BUILD_LOG: the "What stays" row still said "next release"; 5b's "Still to do" was stale | Main session |
| 7 | low | The new "What stays" line said "a folder"; Claude Code keeps one per folder it was used in. Its Windows half rests on an earlier session's look at this PC, not on the uninstall test, whose Windows listing was two levels deep | Main session: reworded to "folders … one for each folder you used it in" |
| 8 | low | The contract's criteria 1 and 2 still name 0.1.0 | The acceptance record says which version each criterion's evidence ran on (§4) |

**Second pass (`critic`, the fixes only):** findings 1 and 3 to 7 closed; 8 stays open until
the acceptance record is written. The fix to finding 2 had added a claim of its own, and
that was corrected:
- `RELEASE.md`'s new rule said "the last release whose first runs passed". 0.1.0's didn't all
  pass: Debian took 150.5 s against two minutes, and the Windows clock wasn't a measurement.
  The rule now says "whose first runs were made", and that their results stand as they were.
- The same paragraph said 0.1.1 changed only PyPI's description and the version. The README
  also changed in two places outside the quick start; the paragraph now lists them, and says
  the uninstall test is skipped by the same rule.
- A sentence in BUILD_LOG's 5a entry still said nothing was published.

## 12. As built (2026-11-11)

Built to the plan, by the `builder`, in two rounds (the second for §11's findings). Commit
`02fc3b8`; the record is `docs/BUILD_LOG.md`, "M4 step 5c".

- **The script** is 57 lines, not about 40. It cuts the two sections as one run from
  `## Status` to just before `## License`, and checks five things are there once: the badge,
  the warning, each of the two headings, and that run ending at `License`.
- **Tests:** six in `tests/test_docs.py` (13 cases) and one in `tests/test_release_smoke.py`.
  Two are beyond §5's list, both from the diff review: `License` must follow the two
  sections, and `--check` and the default mode through `main()`.
- **As §4 decided,** `build` doesn't call `--check`. The link checks on `PYPI.md` and the
  code-block refusal were dropped in v2 and aren't there.
- **The README changed in one more place than §4 says:** the speed note's "before M3"
  (§11, finding 5).
- **Released:** Victor uploaded 0.1.1 at 23:50 UTC. PyPI's files have the tested hashes and
  its description is `PYPI.md`. Right after the upload an installer couldn't find 0.1.1: a
  try at 98 s failed and one at 134 s passed (§8's "cached for a few minutes" row, but for
  the index, not the page).
