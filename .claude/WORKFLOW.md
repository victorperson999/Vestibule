# Vestibule — working loop

How a task moves from idea to commit. Every step is done by Claude: the main Opus session does
the thinking and building, read-only Opus subagents give independent review with a fresh
context, Haiku handles mechanical work, and you (Victor) stay on standby for sign-off and the
commit. Diagram: [`WORKFLOWDIAGRAM.md`](WORKFLOWDIAGRAM.md).

```
you
 │
 ▼
MAIN SESSION — Opus 5.5 (architect + implementer)
 │  1. read context (CLAUDE.md, docs/plans/, HISTORY, BUILD_LOG) ──► scout (Haiku): file:line lookups
 │  2. write plan → docs/plans/<milestone-step>.md
 │  3. [plan review] skeptic (Opus) on milestone/step plans → main session weighs findings
 │  4. you sign off → bullet in docs/HISTORY.md
 │  5. build the change
 │  6. gate (Haiku): ruff + mypy + pytest            ── FAIL → back to 5
 │  7. critic (Opus): adversarial diff review
 │  8. verifier (Opus): CONFIRMED / PLAUSIBLE / REFUTED per finding ── confirmed high/med → back to 5
 │  9. update README/docs if user-facing; HISTORY bullet + BUILD_LOG entry
 ▼
you commit (Claude never runs git add/commit/push; it gives you a staging list + message)

stuck (same failure twice / hang / unclear cause) ──► diagnose (Opus) → root cause → back to 5
```

## Who does what

| Role | Model | Effort | Job | Why |
|---|---|---|---|---|
| Main session | Opus 5.5 | xhigh | Plans *and* writes the code | Vestibule's decisions (D1–D4, the S4/S5 sign-offs, budgets, honesty rules) live in this context. Handing implementation to a fresh agent loses them. |
| `scout` | Haiku 5.5 | low | Lookups that return file:line only | Pure search, no judgment needed. Keeps noise out of the main context. |
| `gate` | Haiku 5.5 | low | Runs ruff, mypy, pytest and reports pass/fail | Runs commands only. Keeps long test output out of the main context. |
| `skeptic` | Opus 5.5 | xhigh | Adversarial review of a **plan** | Challenges the design itself before code exists. That is where the step-4 and step-5 redesigns came from. |
| `critic` | Opus 5.5 | high | Adversarial review of a **diff** | Finds how the change fails. |
| `verifier` | Opus 5.5 | high | Confirms or refutes each finding with evidence | Separates finding from judging, so false positives don't become code churn and real bugs don't get waved away. |
| `diagnose` | Opus 5.5 | xhigh | Root cause when stuck | Fresh eyes on a hang or repeated failure, without the main session's sunk-cost reasoning. |

### Models and effort, precisely
- Agent files use the **aliases** `opus` and `haiku`. On the Anthropic API these resolve to the
  latest release in each family: today Opus 5.5 (`claude-opus-5-5`) and Haiku 5.5
  (`claude-haiku-5-5`, which needs Claude Code ≥ v2.1.293). When a new version ships, the
  agents move up automatically. To freeze a version, replace the alias with the full ID.
- **Effort** is set per agent with the `effort:` frontmatter field, which overrides the
  session's level. The main session's `xhigh` comes from `effortLevel` in
  `~/.claude/settings.json`.
- The rule behind the levels: effort goes where wrong judgment is expensive. Plan design and
  root-causing a hang get `xhigh`, diff review and verification get `high`, and search and
  running checks get `low`.
- Two ways the field can be overridden:
  - `CLAUDE_CODE_EFFORT_LEVEL` beats the `effort:` field. Don't set it, or every agent gets
    the same level.
  - A spawn call can pass its own `effort`, which also wins. The main session never does this
    unless you ask.
- If a level is ever unsupported, Claude Code falls back to the highest supported level at or
  below it. `max` is valid in agent frontmatter but not in settings files.

All review agents (skeptic, critic, verifier, diagnose) are **read-only toward the repo**.
verifier and diagnose may write throwaway repro scripts in a scratch directory only.

## Keeping Opus-on-Opus review honest

Every reviewer is the same model family as the author, so independence has to be built in:
- **Fresh context, artifacts only.** Reviewers get the plan file or the diff plus file paths.
  They never get the main session's reasoning or a summary of why it's right.
- **Neutral briefs.** Spawn prompts say what to review, not what to conclude. No "I think this
  is fine, just double-check X."
- **Separate roles.** The agent that finds a problem (skeptic/critic) is not the one that judges
  it (verifier), and neither one fixes it (main session).
- **Evidence over assertion.** A finding only drives a code change once the verifier has marked
  it CONFIRMED or PLAUSIBLE with a `path:line` or a repro.
- **Human backstop.** You see every finding the main session rejects, with the reason.

## Rules of the loop

- **Plan before code** (CLAUDE.md). The plan names files, approach, and edge cases, and waits for an explicit OK.
- **When each reviewer runs:**
  - skeptic: every milestone or step plan.
  - critic + verifier: every change that touches `src/`.
  - Docs-only or trivial edits skip critic and verifier.
- **Plan findings** (skeptic) are design arguments. The main session addresses each one in the
  plan, accepting, amending, or rejecting it with a reason, before asking you to sign off. Where
  a finding makes a checkable claim about existing code, send it to verifier.
- **Diff findings** (critic) always go through verifier.
  - CONFIRMED high/med: fix, then repeat from step 6.
  - PLAUSIBLE: fix it, or tell you why not.
  - REFUTED: listed in the summary to you.
- **Re-review after fixes.** If a fix touched more than the finding's lines, run critic again on the new diff.
- **Stuck** (same failure twice, a hang, or an unclear root cause): spawn `diagnose` instead of looping.
- **Done** means:
  - gate is green
  - no CONFIRMED high/med findings are open
  - docs are updated
  - the HISTORY bullet is written
  - the `docs/BUILD_LOG.md` entry is written (commit hash filled in next session)
  - you have a staging list

  After that, "log this session" appends to the CLAUDE.md changelog.

## Agent files

All under `.claude/agents/` (project-scoped): `scout.md`, `gate.md`, `skeptic.md`, `critic.md`,
`verifier.md`, `diagnose.md`.
