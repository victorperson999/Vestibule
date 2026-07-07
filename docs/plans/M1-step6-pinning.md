# M1 step 6 plan — digest pinning + setup-UX polish

Status: **draft — S6-D1/D2/D3 pending user sign-off; S6-D4 approved 2026-07-06.**
Parent contract: `docs/plans/M1-container-backend.md` §2-D2 and §8 (binding).
Review history: two Codex adversarial passes, both 2026-07-06.
*Pass 1* (1 high, 2 medium, 2 low; "sound with amendments") — all folded in. The high
finding: the draft *assumed* the captured manifest-list digest would pass the real
preflight + `--pull never` path, but Docker Desktop's classic vs containerd image
stores treat manifest lists differently — so S6-D1 now requires **proving** the exact
final refs through the real code path after capture, and §2's mitigation is a
validated claim, not an assumed one.
*Pass 2* (1 high, 1 medium; full text `docs/reviews/M1-step6-adversarial-review.md`) —
the high (the proof gate validated only whichever image-store mode this machine
happened to be in, while the plan implicitly claimed both) is closed by **S6-D4**;
the medium was an out-of-scope working-tree typo in `naive.py`
(`isolation="false"` — not a valid enum value), resolved same day by reverting the
edit and adding a spawn-failure regression test to `tests/test_smoke.py`.
Scope note: step 6 shrank when the revised S5-D3 moved per-run image preflight and
`--pull never` into step 5. What remains is deliberately small: make the image pins
real, and make the first-time setup experience clear.

---

## 1. What this step does, in plain words

Today `config.py` ships tag-only image references (`python:3.12-slim`, `node:22-slim`).
A tag is a *moving pointer*: Docker Hub can re-point it at new bytes any day, so two
users "on the same version of Vestibule" can silently run different sandbox images.
D2 already decided the fix — pin by **digest**, the content hash of the image, so the
bytes can never change under us.

Step 6 does two things:

1. **Pin for real.** Pull both images fresh, capture their actual digests, and make
   `python:3.12-slim@sha256:<64 hex>` / `node:22-slim@sha256:<64 hex>` the shipped
   defaults. Never invented, never copied from a website — read from this machine's
   Docker after a fresh pull.
2. **Polish the setup path.** A brand-new user's first contact with Vestibule is a
   `Blocked:` message. Make every one of those messages carry the exact fix, and give
   the README the one-time setup block (the two pull commands) that D2 promised.

## 2. The one trap this step must not fall into

Once the default is `python:3.12-slim@sha256:X`, the plain command
`docker pull python:3.12-slim` is **no longer guaranteed to satisfy the preflight**:
if Docker Hub has moved the tag since we pinned, that pull fetches digest Y, and
`image inspect ...@sha256:X` still fails — with an image that *looks* present. A user
following a stale instruction would loop forever.

The mitigation: **every pull instruction — in Blocked messages and in docs — uses the
full digest-pinned reference verbatim** (`docker pull python:3.12-slim@sha256:X`).
Pulling by digest fetches exactly the pinned bytes no matter where the tag has
drifted. The preflight message already interpolates the configured ref
(`container.py:_preflight_image`), so it gets this for free; the README block must be
written the same way, and a test pins the message format so it can't regress.

One caution (Codex review): "pull by digest, then preflight passes" is *necessary but
not assumed sufficient* — a runtime could store the pulled object under a name that
`image inspect <configured-ref>` doesn't resolve (Docker Desktop's classic and
containerd image stores differ on manifest lists, and they are separate stores when
switched). So this mitigation is **validated, not trusted**: S6-D1's capture gate
proves the exact documented setup path satisfies the real preflight + run path — under
both Docker Desktop image-store modes (S6-D4) — before the pins ship.

## 3. Decisions to sign off

### S6-D1 — Pin the multi-arch *manifest-list* digest, captured from a fresh pull here

The capture procedure (run at implementation time, results pasted into `config.py`
with a dated comment):

```
docker pull python:3.12-slim
docker inspect --format "{{index .RepoDigests 0}}" python:3.12-slim
docker pull node:22-slim
docker inspect --format "{{index .RepoDigests 0}}" node:22-slim
```

`RepoDigests[0]` after a pull-by-tag is the **manifest-list** digest — the
platform-independent one. Analysis: this is the right digest to pin because the same
pinned ref then works on Intel/AMD *and* Apple-Silicon/ARM hosts (Docker resolves the
list to the right per-platform image at pull time). Pinning the platform-specific
digest instead would make the default silently unusable on ARM Macs — rejected.
- *Rejected: keep tags and "document that they drift".* That re-opens the exact
  supply-chain hole D2 closed; two installs stop being comparable, and a tag re-point
  becomes an invisible sandbox change. Not acceptable for a security tool.
- *Rejected: pin in a lockfile or fetch pins remotely.* A second file (or a network
  fetch) for two strings is machinery without a payoff at this scale; `config.py` is
  already the single source of truth and env-overridable.
- Honest-tradeoff note (goes in the README line, expanded in SECURITY.md at M4):
  pinned bytes never change silently — which also means the pinned image receives
  **no upstream security fixes** until we re-pin. Re-pinning is a normal, reviewed
  release change; users can override via `VESTIBULE_IMAGE_PYTHON`/`_NODE` at any time.

**Capture is not done until the refs are proven through the real code path** (Codex
high finding — image-store modes make "it should resolve" an assumption, not a fact).
Immediately after capture, with the exact final config strings:

```
docker image inspect --format "{{.Id}}" python:3.12-slim@sha256:<captured>
docker run --rm --pull never python:3.12-slim@sha256:<captured> python -c "print(1)"
docker image inspect --format "{{.Id}}" node:22-slim@sha256:<captured>
docker run --rm --pull never node:22-slim@sha256:<captured> node -e "console.log(1)"
```

All four must succeed. One format note the gate itself verifies: `RepoDigests[0]`
comes back as `python@sha256:X` (repo + digest, **no tag**); the shipped ref keeps
the tag for human readability (`python:3.12-slim@sha256:X`) — Docker ignores the tag
when a digest is present, so both resolve identically, but that equivalence is
exactly the kind of assumption the gate exists to prove, not trust.

### S6-D4 (approved 2026-07-06) — the gate runs under *both* Docker Desktop image stores

Codex pass 2's high finding: proving the pins on whichever store mode this machine
happens to be in leaves the other mainstream mode (classic vs containerd) claimed
but untested — a user there could follow the exact documented command and still fail
preflight. So the capture-day gate runs twice: once per store mode (toggle Docker
Desktop's containerd setting, re-pull, re-run the four commands), and the dated
`config.py` comment records the Docker version and **both** proven modes.
Analysis: ~15 minutes of one-time manual toggling settles empirically what the draft
settled by argument; rejected the alternative (narrow support to the proven mode and
document the gap) because it makes a mainstream default configuration formally
unsupported to save a quarter hour. If either mode *fails* the gate, that fallback —
explicit narrowing, with README/Blocked messages naming the proven mode — comes back
to the user as a fresh decision before implementation. It does not ship on hope.

### S6-D2 — Unpinned image overrides get a loud warning, never a refusal

If a user sets `VESTIBULE_IMAGE_PYTHON=my-python:latest` (no `@sha256:`), the
selector logs one clear stderr warning — "image ref is not digest-pinned; the tag can
change underneath you" — and proceeds. Precisely (Codex clarification): the check
covers **both** configured refs (python *and* node) at each selection pass — never
per-run — because selection only ever probes the python image and the node ref would
otherwise stay silently unchecked until its first use. In practice that is once per
process (success caches for the process lifetime); a re-selection after a cooldown or
a dropped cache re-logs it, which is harmless and arguably a feature.

Analysis: overriding images is a legitimate, documented power feature (custom
toolchains, air-gapped mirrors), and it's the user's machine — refusing would break
real workflows to enforce a preference. But *silence* would violate the project's
"misconfiguration is never silent" rule: someone who pins today and edits the env var
tomorrow should be told what they gave up. A once-per-selection log line costs
nothing and fires never for the shipped defaults. Middle ground adopted; both edges
rejected (hard refusal: hostile to dev use; silence: dishonest).

### S6-D3 — No setup/doctor CLI in M1; the first tool call *is* the doctor

Considered: `python -m vestibule.check` — run the selection checklist outside MCP and
print a human-readable environment report (Docker reachable? images present? profile
enforceable?).

Analysis: genuinely attractive for setup UX, and cheap to build since step 5 made the
whole checklist importable. But it duplicates the report surface (two places to keep
honest), adds a maintenance promise the milestone doesn't need, and the MCP-first
path already self-diagnoses: every failure returns the exact fix, and the 30 s
failure cooldown (S5-D1) means "fix it, then just retry" works inside a live session.
**Recommendation: defer** — recorded as an M4 candidate next to the custom image.
Sign-off here is explicitly invited to overrule if the standalone checker feels
worth having now.

## 4. Setup-UX polish (concrete edits, all small)

1. **README gains a "One-time setup" block** (this pays D2's documentation debt —
   it was promised for the README but never written):
   - install → register with the agent → the two exact digest-pinned pull commands
     → first `run_code`.
   - One sentence each: Vestibule never pulls images itself; if a run is ever
     Blocked, the message itself contains the exact command to fix it; pinned images
     don't auto-update (see S6-D1 note); Docker Desktop must be running Linux
     containers, and if its image-store mode (classic vs containerd) was recently
     switched, re-pull — the two stores are separate (Codex low finding).
   - Analysis: putting the *digest-pinned* commands in the README (not the bare
     tags) is what defuses §2's trap; the block stays 10 lines, the full docs pass
     remains step 7's job.
2. **Preflight refusal gains a daemon hedge.** `image inspect` also fails when the
   daemon died mid-session, and today's message then only talks about pulling. Add
   one clause: "…if Docker itself is not responding, start Docker Desktop, then
   retry." Analysis: string-sniffing the runtime's error text to pick one message
   was considered and rejected as fragile (error strings differ across
   Docker/Podman/versions); one hedged sentence covers both causes honestly.
3. **Consistency pass over the setup-refusal messages** (no runtime, unknown
   runtime, unknown backend, image preflight — both its variants — and
   profile-unenforceable): each states
   (a) what failed, (b) the exact command or setting that fixes it, (c) "then
   retry" — because the S5-D1 cooldown makes an in-session retry actually work.
   Analysis: this is a read-and-align pass, not a rewrite; most messages already
   comply, and a test asserts the *shape* (fix + retry) only where it's load-bearing
   (the pull message), not brittle full-string matches everywhere.

## 5. Code changes

| File | Change |
|---|---|
| `src/vestibule/config.py` | `DEFAULT_IMAGE_PYTHON`/`_NODE` become full digest-pinned refs; dated capture comment (commands + date) above them. |
| `src/vestibule/backends/select.py` | S6-D2 warning: on selection, log once per un-pinned configured image ref. |
| `src/vestibule/backends/container.py` | Preflight refusal message: add the daemon hedge (§4.2). |
| `README.md` | New "One-time setup" block with the digest-pinned pull commands (§4.1). |
| `tests/test_select.py` | +2: unpinned override warns (both refs checked) / pinned defaults don't. |
| `tests/test_validation.py` or `test_select.py` | +1: default image refs match `^(python\|node):[\w.\-]+@sha256:[0-9a-f]{64}$`. |
| `tests/test_lifecycle.py` | Preflight-refusal test asserts the message quotes the configured ref *verbatim* (digest included). |
| `tests/test_container.py` | +1 Docker-marked: `_preflight_image` accepts both pinned defaults + one minimal run each (§7.4). |
| `docs/plans/M1-container-backend.md` | §8 amendment note: digests pinned 2026-07-06 per this plan. |
| `docs/HISTORY.md` | Sign-off + build bullets. |

No new config knobs, no new dependencies, no new tools.

## 6. What can go wrong (and what happens)

- **Docker Hub moves the tag after we pin** → users who pull the bare tag still fail
  preflight, but every instruction they actually see carries the digest-pinned
  command, which the S6-D1/S6-D4 gate proved works (§2).
- **Upstream eventually garbage-collects the pinned digest** (rare for official
  images, not impossible) → the pull command fails; escape hatch is the env override,
  fix is a re-pin release. The README's honesty note makes this legible.
- **ARM/Apple-Silicon host** → covered by pinning the manifest-list digest (S6-D1);
  the same ref pulls the right per-platform image.
- **User already pulled the tag while it still matched the pin** → `RepoDigests`
  recorded at pull time include the list digest, so preflight passes without
  re-pulling. A fresh pull is only needed when the local digest differs from the pin.
- **Existing tests** → preflight regexes match on substrings (`pull python:3\.12-slim`
  still matches the digest ref); fixtures built from `Limits()` defaults pick up the
  pins automatically; Docker-marked tests need the pinned images locally, which the
  capture procedure itself guarantees on this machine.
- **Podman** → pull-by-digest and `RepoDigests` work the same; experimental status
  unchanged (D4).

## 7. Tests

Docker-free (run everywhere):
1. Shipped defaults are well-formed digest refs (full-match regex — catches a
   truncated paste, the realistic failure mode of a copy-in pin).
2. Selection with an un-pinned override logs the S6-D2 warning; with the shipped
   defaults it logs nothing.
3. The missing-image refusal quotes the full configured ref (digest and all) as the
   pull command — pins §2's guarantee against regression.

Docker-marked (this machine):
4. One new test that isolates the step-6 mechanics (Codex low finding — the existing
   hello/probe tests *would* fail on a bad pin, but generically, without pointing at
   step 6): `_preflight_image()` accepts both pinned default refs, and one minimal
   run per image under the pinned ref succeeds with `isolation: container`.
   (Analysis: a test that network-pulls the pinned ref was still considered and
   rejected — tests must not touch the network; the fresh pull + S6-D1 proof gate
   during implementation is the verification, recorded in the config comment.)

Gate: all 104 existing tests stay green (103 from step 5 + the naive.py spawn-failure
regression test added 2026-07-06, outside this step's scope); `ruff check` + `mypy`
clean.

## 8. Definition of done

Digests captured from a fresh pull on this machine on the commit day (never copied
from elsewhere) **and proven through the S6-D1 gate under both image-store modes
(S6-D4)** — exact-ref `image inspect` + `run --pull never` for both images, Docker
version and both proven modes recorded in the config comment;
code + tests as one commit (`feat: M1 step 6 — pin sandbox images by digest + setup
UX`); README setup block present; `docs/HISTORY.md` gains the S6 sign-off and done
bullets; contract §8 annotated; CLAUDE.md changelog on session-log request. Step 7
(acceptance suite + full docs pass) is then the only M1 step left.
