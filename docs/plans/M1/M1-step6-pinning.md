# M1 step 6 plan — digest pinning + setup-UX polish

Status: **built 2026-10-10 — signed off v2. S6-D1 (amended), S6-D2 (amended), S6-D3 approved
2026-10-10; S6-D4 approved 2026-07-06 (procedure tightened 2026-10-10, approved with S6-D1).
Proof gate passed in both image stores; gate green (122 passed); critic ×2 + verifier, all
findings fixed (§10).**
Parent contract: `docs/plans/M1/M1-container-backend.md` §2-D2 and §8 (binding).

Review history:
- *Codex pass 1, 2026-07-06* (1 high, 2 medium, 2 low; "sound with amendments") — all
  folded in. The high finding: the draft *assumed* the captured manifest-list digest
  would pass the real preflight + `--pull never` path, but Docker Desktop's classic vs
  containerd image stores treat manifest lists differently — so S6-D1 requires
  **proving** the exact final refs through the real code path after capture.
- *Codex pass 2, 2026-07-06* (1 high, 1 medium; full text
  `docs/reviews/M1-step6-adversarial-review.md`) — the high (the proof gate validated
  only whichever store mode this machine happened to be in) is closed by **S6-D4**; the
  medium was an out-of-scope `naive.py` working-tree typo (`isolation="false"`), reverted
  same day with a regression test in `tests/test_smoke.py`.
- *Refresh + `skeptic` review, 2026-10-10* (first review under the Claude-only loop in
  `.claude/WORKFLOW.md`; 5 medium, 5 low, "sound with amendments"). `verifier` checked
  the ten code/Docker claims: **10 confirmed, 0 refuted** (corrections on four). The
  biggest change: the shipped refs drop the tag (§3, S6-D1) because Docker does not
  verify a tag that sits next to a digest. Every disposition is in §9.

Scope note: step 6 shrank when the revised S5-D3 moved per-run image preflight and
`--pull never` into step 5. What remains is deliberately small: make the image pins
real, and make the first-time setup experience clear and honest.

Environment at refresh (2026-10-10): Docker Desktop 28.5.1 (linux/amd64), **containerd
image store active**; local `python:3.12-slim` / `node:22-slim` were pulled ~2026-07 by
tag and are stale relative to Docker Hub. Baseline gate green: ruff + mypy clean,
104 passed, Docker-marked tests ran. Nothing was ever captured on 2026-07-06; the
capture happens on the build day.

---

## 1. What this step does, in plain words

Today `config.py` ships tag-only image references (`python:3.12-slim`, `node:22-slim`).
A tag is a *moving pointer*: Docker Hub can re-point it at new bytes any day, so two
users "on the same version of Vestibule" can silently run different sandbox images.
D2 already decided the fix — pin by **digest**, the content hash of the image, so the
bytes can never change under us.

Step 6 does two things:

1. **Pin for real.** Pull both images fresh, capture their actual digests, and ship
   `docker.io/library/python@sha256:<64 hex>` / `docker.io/library/node@sha256:<64 hex>`
   as the defaults. Never invented, never copied from a website — read from this
   machine's Docker after a fresh pull, cross-checked, and proven through the real code
   path in both image-store modes.
2. **Polish the setup path.** A brand-new user's first contact with Vestibule is a
   `Blocked:` message. Make every one of those messages say what failed, the exact fix,
   and what to do next (retry vs. restart — they are not the same, see §4.3), and give
   the README the one-time setup block D2 promised.

## 2. The one trap this step must not fall into

Once the default is `docker.io/library/python@sha256:X`, the plain command
`docker pull python:3.12-slim` is **no longer guaranteed to satisfy the preflight**:
if Docker Hub has moved the tag since we pinned, that pull fetches digest Y, and
`image inspect ...@sha256:X` still fails — with an image that *looks* present. A user
following a stale instruction would loop forever.

The mitigation: **every pull instruction — in Blocked messages and in docs — uses the
shipped reference verbatim** (`docker pull docker.io/library/python@sha256:X`). Pulling
by digest fetches exactly the pinned bytes no matter where the tag has drifted. Two
copies of that instruction exist, and both are test-pinned so neither can go stale:
- the preflight refusal interpolates the configured ref (`container.py:_preflight_image`)
  — §7 test 3 asserts the ref appears verbatim;
- the README setup block is a hand-written copy — §7 test 2 asserts `README.md` contains
  `pull <DEFAULT_IMAGE_PYTHON>` and `pull <DEFAULT_IMAGE_NODE>` verbatim, so a future
  re-pin that forgets the README fails the suite (skeptic finding 4).

"Pull by digest, then preflight passes" is *validated, not trusted*: the S6-D1 gate runs
the documented user path (pull the shipped ref into a store that lacks it) and then the
real preflight + full run profile, under both image-store modes (S6-D4), before the pins
ship.

## 3. Decisions to sign off

### S6-D1 (amended 2026-10-10) — Pin the multi-arch index digest, tagless, fully qualified

**What ships:**
```
DEFAULT_IMAGE_PYTHON = "docker.io/library/python@sha256:<captured>"   # python:3.12-slim
DEFAULT_IMAGE_NODE   = "docker.io/library/node@sha256:<captured>"     # node:22-slim
```
The human-readable tag lives in the dated comment above the constants and in the README,
not in the reference.

**Why the manifest-list (index) digest:** it is platform-independent — the same pinned
ref works on Intel/AMD *and* Apple-Silicon/ARM hosts (Docker resolves the index to the
right per-platform image at pull time). Pinning a platform-specific digest would make the
default silently unusable on ARM Macs — rejected.

**Why tagless (the 2026-10-10 amendment, skeptic finding 5, verifier-confirmed):**
- Docker does not check a tag that sits next to a digest. On this host,
  `docker image inspect python:3.11-slim@<the 3.12 digest>` succeeds and returns the 3.12
  image (`python:nonexistent-tag@<digest>` succeeds too; only the repo name is checked).
  So `python:3.12-slim@sha256:X` *looks* self-describing but isn't: a future re-pin that
  pastes a 3.13 digest under the `3.12-slim` label would pass every check while
  mislabelling the sandbox. The label is instead kept honest by tests that check the
  actual bytes (§7 test 1 per-constant format; §7 test 4 asserts the interpreter
  versions inside the pinned images).
- Tag+digest references have historically been rejected by containers/image (the
  library under Podman). Tagless removes that runtime variance, and the plan's earlier
  ref-form fallback becomes unnecessary.
- Fully qualified (`docker.io/library/…`) avoids Podman's short-name resolution
  (registries.conf / short-name mode). Docker accepts this form directly (verified on
  this host for both `python@…` and `docker.io/library/python@…`).
- Pre-stated fallback (no fresh decision needed): if the fully qualified form fails the
  gate in either store mode but the short `python@sha256:X` form passes, ship the short
  form — same repo, same bytes. If both fail, S6-D4's fallback applies.

**Capture procedure** (containerd mode — this machine's current store):
```
docker pull python:3.12-slim                 # note the printed "Digest: sha256:<D>" line
docker image inspect --format "{{json .RepoDigests}}" python:3.12-slim
docker buildx imagetools inspect docker.io/library/python@sha256:<D>
(same three for node:22-slim)
```
Acceptance, per image — all must hold before anything is pasted:
1. **Two independent readings agree:** the `Digest:` line printed by the pull equals the
   digest of a `RepoDigests` entry. (`RepoDigests` is a list; an image pulled earlier
   under another reference can carry several entries, so "index 0" alone proves nothing.)
2. **It is a multi-platform index:** `imagetools inspect` reports an index/manifest-list
   media type and lists both `linux/amd64` and `linux/arm64` (skeptic finding 7 — the
   ARM claim above is verified at capture, not assumed).

**Proof gate** — runs once per store mode (S6-D4):
1. Precondition: `docker image inspect <shipped ref>` **fails** (the store lacks the
   image; remove it first if needed). This makes the next step prove the documented user
   path — pull by the shipped ref into an empty store — rather than inspect-by-digest
   against an image that was pulled by tag (skeptic finding 1).
2. `docker pull <shipped ref>` for both images (exactly the README / Blocked command).
3. ```
   docker image inspect --format "{{.Id}}" <shipped python ref>
   docker run --rm --pull never <shipped python ref> python -c "print(1)"
   docker image inspect --format "{{.Id}}" <shipped node ref>
   docker run --rm --pull never <shipped node ref> node -e "console.log(1)"
   ```
4. `pytest -m docker` — the real `_preflight_image` + full §3 run profile + selection
   probe, plus the new §7 test 4 (interpreter versions), all under the shipped refs.

All four must pass in each mode before the pins ship.

**Rejected:**
- *Keep tags and "document that they drift".* Re-opens the supply-chain hole D2 closed;
  a tag re-point becomes an invisible sandbox change. Not acceptable for a security tool.
- *Tag + digest (`python:3.12-slim@sha256:X`)* — the v1 choice; rejected 2026-10-10 for
  the reasons above (unverified label, runtime variance).
- *Pin in a lockfile or fetch pins remotely.* Machinery without a payoff for two strings;
  `config.py` is already the single source of truth and env-overridable.

**Honest tradeoff** (README line now, expanded in SECURITY.md at M4): pinned bytes never
change silently — which also means the pinned image receives **no upstream security
fixes** until we re-pin. Re-pinning is a normal, reviewed release change; users can
override via `VESTIBULE_IMAGE_PYTHON` / `_NODE` at any time.

**Re-pin cost** (skeptic finding 10): the two-store proof answers "does this *reference
form* resolve in both stores?", which does not change from one digest to the next. So
the S6-D4 two-mode run is **one-time per reference form**. A routine re-pin of the same
form needs: fresh capture + both acceptance checks + the proof gate in one mode + the
full test suite (which includes the README-copy and interpreter-version tests). This keeps
re-pinning for upstream CVEs cheap enough that it actually happens.

### S6-D4 (approved 2026-07-06; procedure tightened 2026-10-10) — gate under *both* image stores

Codex pass 2's high finding: proving the pins in whichever store mode this machine
happens to be in leaves the other mainstream mode (classic vs containerd) claimed but
untested. So the proof gate runs under both, and the dated `config.py` comment records
the Docker version and **both** proven modes. If either mode fails, the fallback —
explicit narrowing, with README/Blocked messages naming the proven mode — comes back to
the user as a fresh decision before shipping. It does not ship on hope.

*2026-10-10 tightening* (skeptic finding 1): v1's containerd evidence was "pull by tag,
inspect by digest" and the classic evidence was four hand-typed commands — neither mode
ran the documented user path *and* the real code path. Now both do (proof gate steps
1–4 above). The decision itself is unchanged.

Order — Victor flips the store (Docker Desktop → Settings → General → "Use containerd for
pulling and storing images" → Apply & restart); Claude runs every command:
1. **containerd** (current): capture + acceptance checks; then remove the tag-pulled
   sandbox images (`docker image rm python:3.12-slim node:22-slim`, plus any leftover
   ref the precondition still finds); proof gate steps 1–4.
2. **Victor switches to classic.** Proof gate steps 1–4 (this store is separate, so it
   also proves the README's "re-pull after switching" note).
3. **Victor switches back to containerd** (the machine's normal state). Full definition
   of done runs there: `ruff check`, `mypy src`, full `pytest`.

The images being removed are the two Vestibule sandbox images only, and they are
re-pulled in the same procedure.

### S6-D2 (amended 2026-10-10) — Unpinned image overrides get a warning, never a refusal

If a user sets `VESTIBULE_IMAGE_PYTHON=my-python:latest` (anything not ending in
`@sha256:<64 hex>`), the selector logs one clear **stderr** warning — "image ref is not
digest-pinned; the tag can change underneath you" — and proceeds. Precisely: the check
covers **both** configured refs (python *and* node) once per selection pass — never per
run — because selection only probes the python image and the node ref would otherwise
stay unchecked until its first use. It is skipped when `VESTIBULE_BACKEND=naive` (no
images are used). In practice it fires once per process (success caches for the process
lifetime); a re-selection after a cooldown or a dropped cache re-logs it, which is
harmless.

Analysis: overriding images is a legitimate power feature (custom toolchains, air-gapped
mirrors), and it's the user's machine — refusing would break real workflows to enforce a
preference. But *silence* would violate "misconfiguration is never silent". Middle ground
adopted; both edges rejected (hard refusal: hostile to dev use; silence: dishonest).

*2026-10-10 amendments* (skeptic finding 8):
- Wording corrected: this is a stderr log line, which MCP hosts route to a log the user
  rarely opens — it is *a warning*, not a "loud" one.
- **Not surfaced in run results — rejected with reason:** the result's `isolation:` field
  reports what protected the run. An unpinned image weakens *reproducibility/provenance*,
  not isolation (every hard control is still verified by the selection probe), so putting
  it there would blur the honest-isolation vocabulary. Per-run provenance (which image
  ran) belongs in M3's audit record; noted there as a candidate.
- **The override contract gets documented** (one README line): an override image must
  provide `bash` (the selection probe uses it) and the interpreter for its language on
  `PATH`, and must work as an arbitrary non-root uid on a read-only root filesystem.

### S6-D3 — No setup/doctor CLI in M1; the first tool call *is* the doctor

Considered: `python -m vestibule.check` — run the selection checklist outside MCP and
print a human-readable environment report (Docker reachable? images present? profile
enforceable?).

Analysis: attractive for setup UX, and cheap since step 5 made the checklist importable.
But it duplicates the report surface (two places to keep honest) and adds a maintenance
promise the milestone doesn't need. The MCP-first path already self-diagnoses: every
setup refusal names the fix and says whether a retry or a restart is needed (§4.3), and
a mid-session break (e.g. image removed or store switched → `docker run` exit 125) is
explained on the very next call, because the failed run drops the cached selection and
re-checks (wording corrected 2026-10-10, skeptic finding 6: v1 claimed "every failure
returns the exact fix", which the exit-125 path did not). **Recommendation: defer** —
recorded as an M4 candidate next to the custom image. Sign-off may overrule.

## 4. Setup-UX polish (concrete edits, all small)

### 4.1 README

1. **New "One-time setup" block** (pays D2's documentation debt):
   - install from a clone into a venv (`pip install .` — not yet on PyPI) → register with
     the agent (`claude mcp add vestibule -- <venv>/…/vestibule-mcp`, details in
     `docs/GETTING_STARTED.md`) → the two exact pull commands (shipped refs verbatim,
     test-pinned per §2) → first `run_code`.
   - One sentence each: Vestibule never pulls images itself; a Blocked message contains
     the exact fix; pinned images don't auto-update (S6-D1 tradeoff); Docker Desktop must
     be running Linux containers; switching Docker Desktop's image store (classic ↔
     containerd) means re-pulling, because the stores are separate; the override
     contract (S6-D2).
   - Kept to ~12 lines; the full docs pass is step 7.
2. **Fix the stale Status lines next to it** (skeptic finding 9, verifier-corrected):
   the callout ("the only backend that exists today applies no isolation") and the M1
   row ("designed, next up") are false since step 5. The "not yet published to PyPI"
   line is still true and stays.

### 4.2 Preflight refusal hedge

`image inspect` also fails when the daemon died mid-session, and today's message only
talks about pulling. Add one clause naming the configured runtime, not a product:
"…; if `<runtime>` itself is not responding, start it (<runtime-specific how>), then
retry." The how is per runtime — Docker Desktop for docker, `podman machine start` for
podman, nothing for anything else (corrected after `critic`: the v2 example hard-coded
Docker Desktop, which would point a Podman user at the wrong product). String-sniffing
the runtime's error to pick one message was considered and rejected as fragile across
Docker/Podman/versions.

### 4.3 Message pass: retry vs. restart (corrected 2026-10-10, skeptic finding 2)

v1 said every setup refusal should end in "then retry". `verifier` showed that is false
for some of them. The rule is now: **each refusal says what failed, the exact fix, and
the correct next action** — and the next action depends on the cause:

| Refusal | Why "retry" alone is wrong | Next action the message states |
|---|---|---|
| unknown `VESTIBULE_BACKEND` / `VESTIBULE_RUNTIME` (`select.py:136, 179`) | config is read once at server start (`server.py:28`); the process environment is fixed when the agent launches it | fix the MCP server config, then **restart the Vestibule server** |
| no usable runtime, every candidate "not installed" (`select.py:191`) | a running process keeps the PATH it was started with (verified on Windows), so a fresh install is invisible to it | install Docker (or Podman), then **restart your agent** |
| no usable runtime, daemon not reachable | — | start Docker Desktop (or the runtime's daemon), then retry |
| any *retryable* refusal served from the S5-D1 cooldown cache (`select.py:100-102`) | a fix made within 30 s gets the identical refusal; the model may report the fix as failed | annotate: "setup is re-checked at most every 30 s — next check in N s". Restart-only refusals (the two rows above) are cached **unannotated** — waiting cannot fix them (corrected after `critic`) |
| image preflight (`container.py:460`) | — | pull command (shipped ref verbatim) + §4.2 hedge, then retry |
| `docker run` exit 125 (`container.py:280-287`, skeptic finding 6) | today's detail gives no next action — and 125 is ambiguous: a start failure *or* a guest that exited 125 itself | hedged for both causes (corrected after `critic`): no isolation is claimed; if the container failed to start, retry (the next call re-checks setup); if the code exited 125 itself, it did run — do not retry, use another exit code. The v2 text ("nothing was executed. Retry") would have made the model re-run code that already ran |
| probe failed even with soft limits off (`select.py:171`) | message blames the runtime, but a bad override image (no `bash`) fails the same way | neutral wording: the probe failed; either the runtime cannot enforce the profile or the image cannot run the probe (needs `bash`); refusing to run without isolation |

This is a read-and-align pass, not a rewrite. Tests assert the load-bearing *shapes*
(restart wording, cooldown annotation, verbatim ref) — never brittle full strings.

## 5. Code changes

| File | Change |
|---|---|
| `src/vestibule/config.py` | `DEFAULT_IMAGE_PYTHON` / `_NODE` become the fully qualified digest refs; dated comment above them: capture date, source tags, Docker version, both proven store modes, platforms confirmed. |
| `src/vestibule/backends/select.py` | S6-D2 warning (after the naive branch); §4.3 wording: env-var refusals → restart server; runtime-missing split (all not-installed → install + restart agent; else start + retry); cooldown annotation on cached refusals; neutral probe-failure wording. |
| `src/vestibule/backends/container.py` | §4.2 preflight hedge (runtime-named); exit-125 detail gains the retry hint. |
| `README.md` | "One-time setup" block (§4.1); stale Status callout + M1 row fixed. |
| `pyproject.toml` | `docker` marker description: tests skip without a daemon **or without the pinned images**. |
| `tests/test_container.py` | `_docker_ready` also `image inspect`s both `Limits()` refs and skips with the exact pull commands as the reason (skeptic finding 3 — today a daemon-up/image-missing machine, e.g. the laptop, *fails* instead of skipping); `_plant` gains `--pull never` (today it would silently pull from the network); docstring updated; +1 Docker test (§7 test 4). |
| `tests/test_pins.py` (new) | Docker-free: per-constant ref format (§7 test 1) + README-copy test (§7 test 2). |
| `tests/test_lifecycle.py` | Preflight-refusal test asserts the configured ref *verbatim* (today's `pull python:3\.12-slim` regex would break on a tagless ref) + the §4.2 hedge; exit-125 test asserts the retry hint. |
| `tests/test_select.py` | S6-D2: unpinned override warns (both refs checked), pinned defaults silent, naive skips the check; §4.3 shapes: env-var refusals say restart, all-not-installed says install + restart, cached refusal carries the cooldown annotation, neutral probe-failure wording. |
| `docs/plans/M1/M1-container-backend.md` | §8 amendment note: digests pinned on the capture date per this plan; tagless fully qualified form. |
| `docs/HISTORY.md` | Sign-off + build bullets. |

No new config knobs, no new dependencies, no new tools.

## 6. What can go wrong (and what happens)

- **Docker Hub moves the tag after we pin** → users who pull the bare tag still fail
  preflight, but every instruction they actually see carries the shipped ref, which the
  gate proved works (§2), and both copies are test-pinned.
- **Upstream eventually garbage-collects the pinned digest** (rare for official images)
  → the pull fails; escape hatch is the env override, fix is a re-pin release (cheap, see
  "Re-pin cost"). The README's honesty note makes this legible.
- **ARM/Apple-Silicon host** → covered by pinning the index digest; the arm64 entry is
  checked at capture (S6-D1 acceptance 2), though the gate itself runs on amd64.
- **User already pulled the tag while it still matched the pin** → `RepoDigests`
  recorded at pull time include the index digest, so preflight passes without re-pulling.
- **A second dev machine (laptop) without the pinned images** → Docker-marked tests
  *skip* with the exact pull commands (fixed `_docker_ready`), and `_plant` can no longer
  pull from the network; Docker-free tests run normally.
- **Existing tests** → the preflight-refusal regex is rewritten to the verbatim ref;
  fixtures built from `Limits()` defaults pick up the pins automatically; the Docker-marked
  suite needs the pinned images locally, which the proof gate guarantees on this machine.
- **Podman** → the tagless fully qualified form avoids both known Podman pitfalls
  (tag+digest rejection, short-name resolution). Still experimental and not gated (D4) —
  no Podman on this host.

## 7. Tests

Docker-free (run everywhere):
1. Each shipped default matches *its own* full-match pattern —
   `^docker\.io/library/python@sha256:[0-9a-f]{64}$` and the node equivalent (catches a
   truncated paste and a python/node swap; per skeptic finding 5).
2. `README.md` contains `pull <DEFAULT_IMAGE_PYTHON>` and `pull <DEFAULT_IMAGE_NODE>`
   verbatim (skeptic finding 4).
3. The missing-image refusal quotes the configured ref verbatim as the pull command and
   carries the §4.2 hedge.
4. Selection with an un-pinned override logs the S6-D2 warning, covering both refs; the
   shipped defaults log nothing; `backend=naive` skips the check.
5. §4.3 shapes: env-var refusals say restart; all-not-installed says install + restart;
   a cooldown-cached refusal carries the annotation; the exit-125 detail carries the
   retry hint; the both-probes-failed refusal uses the neutral wording.

Docker-marked (this machine; skip elsewhere with the exact pull commands):
6. `_preflight_image()` accepts both shipped refs, and one minimal run per image under
   the shipped ref succeeds with `isolation: container` **and reports the expected
   interpreter version** (`3.12.x` / `v22.x`) — this is what keeps the "python:3.12-slim"
   label in the config comment honest (skeptic finding 5). Tests never touch the network;
   the capture + proof gate during implementation is the network-facing verification,
   recorded in the config comment.

Gate: all 104 existing tests stay green; `ruff check` + `mypy src` clean.

## 8. Definition of done

- Digests captured from a fresh pull on this machine on the build day (never copied from
  elsewhere), passing both acceptance checks, and proven through the proof gate under
  **both** image-store modes (S6-D4); Docker version, modes, date, tags and platforms
  recorded in the `config.py` comment.
- Code + tests per §5; `gate` green in the final (containerd) mode; `critic` + `verifier`
  run on the diff with no confirmed high/medium findings open.
- README setup block present and Status lines corrected; contract §8 annotated;
  `docs/HISTORY.md` gains the S6 sign-off and done bullets.
- Victor gets a staging list + suggested message
  (`feat: M1 step 6 — pin sandbox images by digest + setup UX`); Claude never commits.

Step 7 (acceptance suite + full docs pass) is then the only M1 step left.

## 9. Skeptic findings (2026-10-10) — dispositions

`verifier` verdicts in brackets. "Accepted" means folded into the sections above.

| # | Sev | Finding (short) | Verifier | Disposition |
|---|---|---|---|---|
| 1 | med | Gate never ran the user path (pull by digest into an empty store) on containerd, nor the real code path on classic | — (procedure) | **Accepted** — proof gate precondition + `pytest -m docker` in both modes (S6-D1, S6-D4). |
| 2 | med | "then retry" false for env-var / install refusals; 30 s cooldown hides fixes; hedge hard-codes Docker Desktop | CONFIRMED (env-var part med → low: the real gap is wording — a restart is the only fix) | **Accepted** — §4.3 retry-vs-restart table; §4.2 names the runtime. |
| 3 | med | Docker tests fail (not skip) without the exact images; `_plant` pulls from the network | CONFIRMED ×2 | **Accepted** — `_docker_ready` image check + `--pull never` in `_plant` (§5). |
| 4 | med | README pull commands are an untested copy of the pins | — (design) | **Accepted** — §7 test 2. |
| 5 | med | Tag next to a digest is unverified decoration; tag+digest refs vary across runtimes | CONFIRMED (Docker ignores the tag; repo is checked) | **Accepted** — S6-D1 amended to the tagless, fully qualified form; per-constant regex; interpreter-version test. Supersedes the earlier ref-form fallback. |
| 6 | low | Exit-125 result gives no next action | CONFIRMED (and the next call does re-check) | **Accepted** — retry hint (§4.3); S6-D3 wording corrected. |
| 7 | low | Multi-arch / ARM premise asserted, not checked | — (procedure) | **Accepted** — S6-D1 acceptance check 2. |
| 8 | low | S6-D2 "loud" is stderr-only; override contract undocumented; bad override misdiagnosed; naive should skip | CONFIRMED (the refusal does name the missing `bash`) | **Partly accepted** — wording fixed, contract documented, naive skips, neutral probe-failure wording. **Rejected:** surfacing the warning in run results (it is provenance, not isolation; belongs in M3's audit record). |
| 9 | low | README Status section is stale next to the new block | CONFIRMED except the PyPI line (still true) | **Accepted** — two stale lines fixed; PyPI line kept. |
| 10 | low | Unclear whether every re-pin needs the two-store toggle | — (design) | **Accepted** — two-mode proof is one-time per reference form ("Re-pin cost"). |

## 10. Critic findings on the diff (2026-10-10) — dispositions

`critic` reviewed the built diff (1 medium, 2 low); `verifier` confirmed all three with
repros (one against the real daemon). All fixed in the same step; gate re-run after.

| # | Sev | Finding (short) | Fix |
|---|---|---|---|
| 1 | med | The new exit-125 detail said "nothing was executed. Retry" — but a guest that exits 125 itself lands on the same path, so the model would re-run code that already ran (verified live with `echo hi; exit 125`) | Hedged detail covering both causes (§4.3 row); the step-5 classification (`isolation: none`, re-probe next call) is unchanged. Test asserts both next-actions and the absence of "nothing was executed". |
| 2 | low | The cooldown annotation was added to restart-only refusals too, implying waiting helps | Restart-only refusals raise a `_RestartRequired` subclass and are cached unannotated (§4.3 row); parametrized test over all three restart cases. |
| 3 | low | The preflight hedge hard-coded Docker Desktop for every runtime | Runtime-specific start hint (§4.2); test for docker vs podman wording. |

**Pre-existing issue surfaced (not a step-6 change; deferred to the step-7 plan):** a
selection dropped by `note_result` is never `drain()`ed, and the server never drains its
live warden at shutdown either (`asyncio.run(_main())` mass-cancels leftovers). Detached
cleanup tasks caught mid-spawn at loop shutdown can hit the Windows proactor deadlock that
`drain()` exists to prevent, and the replacement backend's fresh semaphore does not count
the dropped one's still-cleaning containers (transient, bounded by cleanup timeouts).
Confirmed by `verifier` (1 pending finisher observed right after `note_result`). It
belongs to the step-4/5 lifecycle and needs its own design + `skeptic` pass, so it goes
into the step-7 plan rather than being patched here outside a signed-off plan.

**Round 2** (`critic` re-review after the fixes above; `verifier` confirmed all four):

| # | Sev | Finding (short) | Fix |
|---|---|---|---|
| 4 | med | README registered the server with `claude mcp add` at the default `local` scope — only usable inside the clone, silently absent everywhere else | `--scope user` + a one-line note in the setup block. (`docs/GETTING_STARTED.md` is the dev guide where repo-local registration is intended; its pass is step 7.) |
| 5 | low | "not installed" refusal assumed a fresh install; it really means "not on this process's PATH" (also: installed-but-off-PATH, mis-set `VESTIBULE_RUNTIME`) | Message names all three; still restart-only. |
| 6 | low | Runtime-resolution refusal still hard-coded Docker Desktop for every runtime (the same misdirection fix 3 removed from the preflight) | One shared `START_HINTS` table (`container.py`) used by both paths; `_version_check` no longer asks "is Docker Desktop running?". |
| 7 | info | HISTORY / BUILD_LOG still said "in progress" | Not a defect — loop step 9 (docs) runs after review; done below. |

No third `critic` round: the round-2 fixes stay inside the flagged message sites (plus the
one shared hint table), and each has its own test.
