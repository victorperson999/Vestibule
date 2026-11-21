# M4 follow-up — a README a first-time user can follow top to bottom

Status: **v2, signed off by Victor 2026-11-20** ("Looks good"): the top of the page stays as
signed, and the permanent id test (§8) isn't added. **Built 2026-11-20 (§10 is its as-built).**
v1 was reviewed by
`skeptic` (12 findings: 1 high, 6 medium, 5 low; verdict "sound with amendments"). The main session
checked each claim the findings make about the repo against the files, and all held. Every
finding is taken (§9). Docs only: no `src/`, no `tests/`, no release. Victor's choices,
2026-11-20: the reference material moves out of the README into a new `docs/REFERENCE.md`; the XERJ
contributor badge stays where it is.

## 1. The problem

The README (368 lines, about 5,500 words) is accurate, but a first run has more to get through
than it needs:
- **Basic facts are missing.** The languages (Python, Bash, Node) appear only in the image rows
  of the Configuration table. `read_workspace` is never introduced. Every platform's last step
  says "check the `isolation:` line", and the page never shows a result, or says how to see the
  line when the agent doesn't show it (BUILD_LOG, M4 step 5a: "The `isolation:` line the README
  says to check wasn't on screen").
- **The quick start makes you jump.** Ubuntu says "the same commands as in the Linux section
  below". Ubuntu under WSL2 is sent from the Ubuntu section to the Linux one. The check that ends
  every platform is written once, above all four, instead of as each platform's last step.
- **Reference material is half the words.** Native isolation (790 words), usage numbers and the
  audit log (628), Configuration (537) and What `setup` does (362), most of it for an operator,
  not a first run.
- **Two sections sit in odd places.** Uninstall is between "What it protects" and the reference
  sections. First-run troubleshooting is a `###` inside Connect a client.

**What this step doesn't change: the top of the page.** v1 moved the sandbox-only section below
the quick start and cut the GIF's caption to one line. Both are signed decisions: F4 puts the
boundary second, before the quick start (`M4-adoption.md`, F4; HISTORY calls it one of the two
changes that "changed the plan most"), and P7 with its 4b and 5d amendments says what the caption
holds. The caption also names "the stricter line from What is and isn't sandboxed", so that
section can't lose it. Changing either is an amendment for Victor (§8).

## 2. Rules this step keeps

- **No command changes.** Every command a first run, the sandbox-only session or the uninstall
  types stays byte for byte, in the same order, in fenced blocks and inline code alike. A code
  block may be split in two at a line boundary. So the first runs and uninstall tests on the
  fresh VMs (HISTORY, steps 5a–5d) still describe what the page says. The page on `main` keeps
  describing 0.1.2, whose `src/` is `HEAD`'s (`git diff --stat 430c1b6 HEAD -- src` is empty).
- **No claim is dropped or made stronger** (golden rule 5). Every caveat and every "tested",
  "untested", "measured" and "expected" stays. Text that moves to `REFERENCE.md` moves verbatim,
  except its links (§4). Sections this plan doesn't name as changed stay as they are.
- **F4's order and P7's caption stay** as signed (§1).
- **S5-D3's sentence stays:** each platform ends by asking the agent to run `print(1)` with
  Vestibule's `run_code` tool.
- **The seven heading ids that published PyPI pages link to keep a heading** in the README:
  `#what-is-and-isnt-sandboxed`, `#connect-a-client`, `#ubuntu-2310-and-later`, `#configuration`,
  `#what-setup-does`, `#usage-numbers-and-the-audit-log` (0.1.0 to 0.1.2) and `#status` (0.1.0,
  whose page was the README at `4bd503c`). Those pages can't be edited. **Each id also keeps what
  its published link text promises, or links straight to it:** "The rules for an override are
  under What `setup` does" and "What the records keep, and who can read them: Usage numbers and
  the audit log" are on those pages.
- **What the tests and the PyPI script need,** unchanged:
  - every link is an `https://` URL, and every `#id` it names resolves (`test_docs.py`);
  - `docs/demo-2.gif` is the only GIF shown;
  - the two `docker pull` lines and the `docker image rm` line stay in the README, verbatim
    (`test_pins.py`, and `docs/RELEASE.md` §2 step 5, which also edits the "as of" date beside
    them);
  - the status badge and the `> ⚠️ **Early development` line appear once each, and
    `## Status`, `## Documentation`, `## License` end the page in that order
    (`scripts/pypi_readme.py`).

## 3. The new README, in order

| # | Section | What changes | Today |
|---|---|---|---|
| 1 | Head, the analogy, the diagram and the line under it | Nothing. | `:1–28` |
| 2 | Why use Vestibule? | One sentence added to "What it is": the code runs as Python 3.12, Node 22 or Bash, and a second tool, `read_workspace`, reads the files a run leaves in the workspace. The early-development callout is unchanged. | `:32–46` |
| 3 | What is and isn't sandboxed | Nothing (F4, P7). | `:48–71` |
| 4 | Quick start | The intro paragraph gains a table: your machine → what you need first → the isolation you'll get, each cell marked as Where native works marks it (measured, expected, untested), linking that table. `### Windows`, `### Ubuntu 23.10 and later` and `### Linux with systemd (Debian, Fedora, Arch, WSL2)` become numbered steps, each complete on its own and ending with S5-D3's check and a link to Check it works. Ubuntu repeats four lines from the Linux section in place of its pointer sentence, exactly: `curl -LsSf https://astral.sh/uv/install.sh \| sh`, `uv tool install vestibule-mcp`, `vestibule-mcp setup`, `claude mcp add --scope user vestibule -- vestibule-mcp`. Nothing else is copied: Debian's `ca-certificates … dbus-user-session` block stays Linux-only. **`### macOS` stays as it is**, because its steps have never been run and numbered commands would present them as if they had. | `:73–137` |
| 5 | Check it works | New. Two real `run_code` results for `print(1)`, one `native` and one `container` (§5). Claude Code folds tool results, and an agent may summarise instead of quoting (BUILD_LOG 5a: "output 1, exit code 0"). **So the check that doesn't rest on the model's copy is `vestibule-mcp audit` in a terminal,** whose last line shows `isolation=` from the server's own record, with a real line from the same run. Asking the agent to repeat the result is offered only as a convenience. A table of what each `isolation:` value means, plus `Blocked:`. | new; values from `:330` and GETTING_STARTED's Troubleshooting |
| 6 | Connect a client | Nothing. | `:139–172` |
| 7 | First-run troubleshooting | Raised from `###` to `##`. Same title, so the same id. | `:174–182` |
| 8 | What it protects, and what it doesn't | Nothing. | `:184–191` |
| 9 | Configuration | Short. Its first line links to the full table in `REFERENCE.md`. Then: where the settings go and that a restart reads them, what an invalid value does, and the names people change most (`VESTIBULE_WORKSPACE`, `VESTIBULE_WORKSPACE_RO`, `VESTIBULE_BACKEND`, `VESTIBULE_MEM_MB`, `VESTIBULE_CPU_PCT`, `VESTIBULE_DEFAULT_TIMEOUT_S`). No defaults are repeated, so the two can't drift. | `:240–262` |
| 10 | What `setup` does | Short. Its first line links to "What `setup` does, in detail", **and one line says the rules for a custom image are there**, which is what the published pages promise. It keeps: the opening paragraph, the `Blocked:` bullet, the `vestibule-mcp audit` bullet verbatim (with "printed escaped"), the run-as-yourself bullet, the 357 MB bullet, the "as of" bullet, and the two `docker pull` lines with their sentence. | `:219–238` |
| 11 | Usage numbers and the audit log | Short. Its first line links to the full section. It keeps one sentence each on the `usage:` line, what a record keeps (the code up to 4 KiB, never the output), who can read it, that a failed write never stops a run, that it isn't tamper-proof, and that nothing deletes old records, each taken from today's bullet. | `:310–326` |
| 12 | Uninstall | Nothing, moved down to here. | `:193–217` |
| 13 | How it works (design) | Nothing. | `:328–332` |
| 14 | Status | Nothing. | `:334–349` |
| 15 | Documentation | `REFERENCE.md` gets a row, after `SECURITY.md`'s. | `:351–364` |
| 16 | License | Nothing. | `:366–368` |

**Size:** about 3,900 words, down from 5,500, nearly all of it from moving the reference. The first
install command moves from line 86 to about line 95, because the quick start gains its table and
the top doesn't change. What gets shorter is the part after the quick start.

## 4. `docs/REFERENCE.md` (new)

Its first line: "The details behind the README, written for vestibule-mcp 0.1.2." This page lives
on `main`, while each release's PyPI page is frozen, so a 0.1.2 reader who follows a link here
reads `main`. The version line says which release the page describes, and `docs/RELEASE.md` gets
one checklist line: the candidate updates it. `test_docs.py` allows only links on `main`, so the
links can't name a tag instead.

Its sections, moved verbatim from the README:
- **Configuration:** the whole of today's section, the intro paragraph on how values are read
  and the 17-row table.
- **What `setup` does, in detail:** the custom-image rules, the image-store note, and the
  `VESTIBULE_ROOTFS_DIR` / `VESTIBULE_IMAGE_*` sentence. The "as of" date and the `docker pull`
  lines stay in the README only, so a re-pin edits one file (§2).
- **Native isolation on Linux:** all of it, What `auto` picks, Speed, Where native works and the
  prerequisites included.
- **Usage numbers and the audit log:** all of it.

**Links in moved text:**
- Relative, as everywhere in the repo but the README.
- **A link between two moved sections points inside `REFERENCE.md`.** Examples: the
  `VESTIBULE_IMAGE_PYTHON` row's "The rules for an override are under What `setup` does", and
  the `VESTIBULE_AUDIT_DIR` row's "more".
- A link to something that stays in the README points at `../README.md#<id>`.

## 5. The example results in Check it works

Captured, not written: one `run_code` call with `{"language": "python", "code": "print(1)"}`
on each side, then `vestibule-mcp audit` on the same audit folder.
- **The server:** 0.1.2 from PyPI, installed with `uv` into a throwaway venv, not a tool install
  and not the dev tree. It's driven over stdio by `tests/stdioutil.py`'s client (`exe=` that
  venv's `vestibule-mcp`), from a throwaway script.
- **Native:** in WSL (Ubuntu 26.04), with the venv, workspace and audit folder in a Linux-side
  temp dir, never under `/mnt/c`.
- **Container:** on Windows with Docker Desktop, in a temp dir.
- Each is pasted exactly, with one line under it saying where, when and which version. If
  either result has an `audit:` line or anything else unexpected, that's a finding about the
  capture, not something to edit out.

## 6. Other files

- `PYPI.md`: regenerated with `scripts/pypi_readme.py`.
- `SECURITY.md:51`: "the README's 'Where native works'" becomes a link to that section of
  `docs/REFERENCE.md`.
- `docs/GETTING_STARTED.md:45`: "listed in the README's **Configuration** table" becomes a link
  to `REFERENCE.md`'s Configuration.
- `docs/RELEASE.md`: the one line of §4.
- `docs/HISTORY.md`: a sign-off bullet and a done bullet under M4's build steps.
- `docs/BUILD_LOG.md`: one new row and one entry. Victor's uncommitted edits to it (the hashes
  for 5d and M4 accepted) are left as they are.

## 7. How it's checked

- **Tests:** `pytest tests/test_docs.py tests/test_pins.py tests/test_release_smoke.py` on
  Windows, and `python scripts/pypi_readme.py --check`.
- **Commands unchanged, in order:** a throwaway script extracts, per section, every fenced line
  and every inline code span, in order, from today's README and from the new README plus
  `REFERENCE.md`. It covers each platform of the quick start, What is and isn't sandboxed,
  Connect a client, First-run troubleshooting and Uninstall. Each sequence must be equal, with
  these expected differences, and no others:
  - Ubuntu's sequence gains exactly §3's four lines, where the pointer sentence was;
  - the old quick-start intro's spans (`print(1)`, `run_code`, `isolation:`) reappear in each
    platform's last step.
- **Nothing lost:** a throwaway script removes link targets (`[text](url)` becomes `text`),
  normalises whitespace, and lists each sentence of today's README that is in neither the new
  README nor `REFERENCE.md`. Every one must be named in the done bullet with what replaced it.
  Expected: only the old quick-start intro's sentences and Ubuntu's pointer sentence.
- **New text against its sources:** every new sentence (§3 rows 2, 4, 5, 9, 10, 11, and §4's
  first line) is listed with the line it rests on. `critic` reviews the diff with that list, and
  `verifier` checks its findings, as 5d's README diff was reviewed.
- **Ids:** the seven ids of §2 are in the new README's headings. Every `#id` in links from
  `REFERENCE.md`, `SECURITY.md:51` and `GETTING_STARTED.md:45` resolves. Both are checked with
  `_heading_ids` from `test_docs.py`, run by hand, because `test_docs.py` checks ids only for the
  README's links.
- **PyPI's renderer:** `readme_renderer` on `PYPI.md` if it's in the dev venv. If not, it's
  checked by the next release's own checklist, which already draws the page.

## 8. Not in this step

- **A release.** PyPI's page changes only with the next release, which is Victor's call. Until
  then GitHub shows the new page and PyPI 0.1.2's, and both describe the same code.
- **The top of the page.** Shortening the caption needs an amendment to P7 and its 4b
  amendment, which require the caption to list the edits and say the agent's copy was compared.
  Moving What is and isn't sandboxed below the quick start needs an amendment to F4, and the
  caption would have to stop naming it. Victor's call; this plan assumes neither.
- **A permanent test of the seven ids,** as `test_docs.py` pins the published GIFs. It would be
  a `tests/` change, so a builder's. Offered to Victor; not in this plan unless he asks.
- The GIF, `ARCHITECTURE.md`, and `SECURITY.md` beyond its one reference.

## 9. Review (v1 → v2)

`skeptic`, 2026-11-20. Claims about the repo checked by the main session: F4's order
(`M4-adoption.md`), P7 and its amendments, HISTORY's "changed the plan most", step 4's finding
14, BUILD_LOG's 5a run, `audit_cli.py`'s `isolation=`, 0.1.0's page at `4bd503c`, and step 5d's
review finding 2. All confirmed.

| # | Sev | Finding | Change |
|---|---|---|---|
| 1 | high | Moving the boundary and cutting the caption contradict F4 and P7, and the cut caption would leave a `native` GIF from a sandbox-only session unexplained on PyPI | Both dropped; the top stays (§1, §3 rows 1–3); an amendment is Victor's call (§8) |
| 2 | med | "Repeat the result word for word" checks text the model typed | `vestibule-mcp audit` is the check; repeating is a convenience (§3 row 5) |
| 3 | med | Moved sections' links would land on short README sections that no longer hold what the link text promises, on `main` and on published pages | Links between moved sections stay inside `REFERENCE.md`; each kept id keeps or links to its promise (§2, §3 rows 10–11, §4) |
| 4 | med | Rewording the session notes and dropping the Status bullets breaks §2's own rule | Neither is changed (§3 rows 3, 14) |
| 5 | med | The checks skip inline commands and order, the Ubuntu exception is open-ended, the sentence diff can't hold its list, and new prose and non-README ids go unchecked | §7 rewritten: inline spans, order per section, four named lines, link targets stripped, new sentences listed for `critic`, ids by hand |
| 6 | med | "Complete on its own" for macOS would present untested steps as a procedure | macOS stays as it is (§3 row 4) |
| 7 | med | `REFERENCE.md` on `main` may describe a later version than the reader's | The version line, updated by the release checklist (§4, §6) |
| 8 | low | One-word isolation cells overstate the expected rows | Marked as Where native works marks them (§3 row 4) |
| 9 | low | 0.1.0's page links `#status` too | Seven ids (§2) |
| 10 | low | The id check is one-off | A permanent test offered to Victor (§8) |
| 11 | low | The line estimate can't be met | Corrected (§3, Size) |
| 12 | low | Capturing from `/mnt/c` with the dev tree; native only, when three of four platforms get `container` | 0.1.2 from PyPI, Linux-side temp dir, and both results (§5) |

## 10. As built (2026-11-20)

- **How:** a throwaway script built the new README and `docs/REFERENCE.md` from a copy of the old
  README, copying every unchanged or moved line by number, so nothing was retyped. `PYPI.md` was
  regenerated with `scripts/pypi_readme.py`.
- **The captures (§5):** both from vestibule-mcp 0.1.2 off PyPI, on 2026-11-20. Native in WSL2
  (Ubuntu 26.04.1, kernel 6.6.87.2), with the venv, workspace and audit folder under `~/`, never
  `/mnt/c`. Container on Windows 11 with Docker Desktop (Engine 28.5.1). **One departure:** on
  Windows the venv was made with `python -m venv` and `pip`, not `uv`, because the WinGet `uv`
  wouldn't launch from the session's shell. It's the same package from PyPI either way. The
  README's audit line was checked against a second `vestibule-mcp audit` on the same folder:
  identical.
- **§7's checks:**
  - **Commands:** fenced lines and inline code spans were compared as two ordered sequences
    per section, not one, because splitting a block at a line boundary moves prose spans
    between fenced lines. Every fenced line is unchanged and in order. The only additions are
    Ubuntu's four lines and the example results.
  - **Span differences beyond §7's list,** none of them a command:
    - `curl` in Ubuntu's step title;
    - the quick-start table's spans (`container` ×3, `curl` ×2, `native`);
    - What `setup` does' moved spans, which are in `REFERENCE.md` under a section with a
      different title.
  - **Sentences removed,** each replaced by the numbered steps that say the same thing: the
    old quick-start intro's three; "Before you start", "Install and connect" and "Expect" in
    Windows, Ubuntu and Linux; and Ubuntu's pointer to the Linux section. Every other sentence
    of the old README is in the new README or in `REFERENCE.md`.
  - **New sentences:** `critic` found each one's support in the repo itself instead of being
    handed a list of sources.
  - **Ids:** all seven published ids are headings, and every `#id` linked from `REFERENCE.md`,
    `SECURITY.md` and `GETTING_STARTED.md` resolves.
  - **Tests:** `test_docs.py`, `test_pins.py` and `test_release_smoke.py` passed (112, Windows),
    and so did `pypi_readme.py --check`.
  - **Not run:** `readme_renderer` isn't in the dev venv, so PyPI's rendering is left to the
    next release's checklist.
- **Size:** the README is 4,525 words and 381 lines, against about 3,900 and roughly 320. The
  quick-start table, Ubuntu's repeated steps, Check it works and the short sections came to more
  than estimated. The first install command is at line 93. `REFERENCE.md` is 2,071 words.
- **The diff review:** `critic` found 7 (1 high, 2 medium, 4 low), and `verifier` confirmed all
  7, lowering four of them (the high one to medium, the two mediums to low). Each was fixed in
  its own lines, so no second `critic` pass:
  1. The `none` row said only `naive` or a run that never started. `none` also comes back when
     hard controls weren't confirmed or the sandbox didn't report back, after the code may have
     run (`native.py`, `server.py`). The row now says only `nothing was executed` means the code
     didn't run.
  2. The table's Linux row lost "Fedora 38 and later" and arm64's "untested". Both added.
  3. "One line per call" overstated `audit`. It now says each call that reached Vestibule, says
     to check the line's time, and links the two kinds of call that leave none. A `Blocked:`
     row shows a reason, not an isolation.
  4. Not every `Blocked:` says whether to retry or restart (the validation refusals don't).
     The row now says "where it matters".
  5. "Docker Desktop 28.5.1" is the Engine's version. The caption now says so.
  6. `docs/PLAN.md` said the README quotes the speed numbers, which moved. It now names
     `REFERENCE.md`.
  7. The span differences beyond §7's list. They're named above.
- **Left as is, noted:**
  - What `setup` does' bullet "the message names the exact fix and says whether to retry or
    restart" has finding 4's imprecision. It's an unchanged line.
  - `REFERENCE.md`'s Speed line says "Docker Desktop 28.5.1", finding 5's wording. It moved
    verbatim.
