# Vestibule — workflow diagram

Visual companion to [`WORKFLOW.md`](WORKFLOW.md). Box colors: purple = Opus, green = Haiku,
blue = you (Victor).

## Flowchart (renders on GitHub / VS Code preview)

```mermaid
flowchart TD
    YOU([You: task / idea]):::you --> CTX

    subgraph MAIN["MAIN SESSION — Opus 5.5 · xhigh (architect + implementer)"]
        CTX[1. Read context<br/>CLAUDE.md · docs/plans · HISTORY]
        PLAN[2. Write plan<br/>docs/plans/&lt;step&gt;.md<br/>files · approach · edge cases]
        WEIGH[Address each plan finding<br/>accept · amend · reject + reason]
        BUILD[5. Build the change]
        DOCS[9. Update README/docs<br/>+ HISTORY bullet]
    end

    CTX <-. "where is X?" .-> SCOUT[scout · Haiku · low<br/>file:line only]:::haiku
    BUILD <-. lookups .-> SCOUT

    CTX --> PLAN
    PLAN --> PGATE{Milestone or<br/>step plan?}
    PGATE -- yes --> SKEPTIC[3. skeptic · Opus · xhigh<br/>fresh context, read-only<br/>is this the right design?]:::opus
    SKEPTIC -- findings --> WEIGH
    WEIGH -- plan revised --> SIGN
    PGATE -- no --> SIGN{{4. You sign off<br/>logged in HISTORY}}:::you
    SIGN -- changes requested --> PLAN
    SIGN -- approved --> BUILD

    BUILD --> GATE[6. gate · Haiku · low<br/>ruff · mypy · pytest]:::haiku
    GATE -- FAIL --> BUILD
    GATE -- PASS --> SRC{Touches src/?}
    SRC -- no --> DOCS
    SRC -- yes --> CRITIC[7. critic · Opus · high<br/>fresh context, read-only<br/>assume it's broken]:::opus
    CRITIC -- "no issues found" --> DOCS
    CRITIC -- findings --> VERIFIER[8. verifier · Opus · high<br/>trace + repro each finding<br/>CONFIRMED · PLAUSIBLE · REFUTED]:::opus
    VERIFIER -- "confirmed high/med" --> BUILD
    VERIFIER -- "all refuted / low<br/>(reported to you)" --> DOCS

    DOCS --> DONE([Staging list + commit message]):::you
    DONE --> COMMIT([You commit]):::you

    BUILD -. "same failure twice /<br/>hang / unclear cause" .-> DIAG[diagnose · Opus · xhigh<br/>fresh-context root cause]:::opus
    DIAG -. "root cause + fix direction" .-> BUILD

    classDef you fill:#1e3a5f,stroke:#5b9bd5,color:#fff
    classDef opus fill:#3d2a5c,stroke:#a77bdb,color:#fff
    classDef haiku fill:#1f4d2e,stroke:#5cb85c,color:#fff
    style MAIN fill:#2a1f3d,stroke:#a77bdb,color:#fff
```

## Plain-text version (for the terminal)

```
                         you: task / idea
                                │
                                ▼
 ┌──────────────── MAIN SESSION · Opus 5.5 ────────────────┐      ┌──────────────────┐
 │  1. read context (CLAUDE.md, plans, HISTORY) ◄───────────┼─────►│ scout · Haiku    │
 │                     │                                    │      │ file:line only   │
 │                     ▼                                    │      └──────────────────┘
 │  2. write plan → docs/plans/<step>.md                    │
 └─────────────────────┬────────────────────────────────────┘
                       ▼
        milestone/step plan? ──yes──► 3. skeptic · Opus (read-only)
                       │                     │ findings
                       │                     ▼
                       │          main session: accept / amend / reject + reason
                       no                    │
                       ▼                     ▼
        4. YOU SIGN OFF (logged in HISTORY) ◄┘
                       │  changes requested → back to 2
                       ▼
 ┌──────────────── MAIN SESSION · Opus 5.5 ────────────────┐
 │  5. build the change ◄───────────────────────────────┐   │ ◄─ stuck? ─► diagnose · Opus
 └─────────────────────┬────────────────────────────────┼───┘            (root cause only)
                       ▼                                │
        6. gate · Haiku: ruff · mypy · pytest ──FAIL────┤
                       │ PASS                           │
                       ▼                                │
        touches src/? ──no──────────────────┐           │
                       │ yes                │           │
                       ▼                    │           │
        7. critic · Opus (read-only)        │           │
                       │ findings           │           │
                       ▼                    │           │
        8. verifier · Opus ─CONFIRMED high/med──────────┘
                       │ refuted / low      │
                       ▼                    ▼
        9. README/docs + HISTORY bullet ◄───┘
                       │
                       ▼
        staging list + commit message  ──►  YOU COMMIT
```

## Who runs on what

```
 Opus 5.5   ██████  main session (plan + build), skeptic, critic, verifier, diagnose
 Haiku 5.5  ██      scout, gate                         ← mechanical, cheap
 You        ██      plan sign-off, final say on rejected findings, git commit
```

## Effort per role

```
 xhigh  ████████  main session · skeptic · diagnose   ← design + root cause: wrong = expensive
 high   ██████    critic · verifier                    ← find + prove defects in a bounded diff
 low    ██        scout · gate                         ← search + run commands, no judgment
```

## Finding → action

| Source | Verdict | Action |
|---|---|---|
| skeptic (plan) | design argument | main session accepts, amends, or rejects with a reason, before your sign-off |
| critic (diff) | CONFIRMED high/med | fix, then repeat from step 6 (re-run critic if the fix spread) |
| critic (diff) | PLAUSIBLE | fix, or explain to you why not |
| critic (diff) | REFUTED / low | listed in the summary to you, no code change |

## Loop exits at a glance

| Loop | Goes back to | Trigger |
|---|---|---|
| Plan loop | step 2 | skeptic findings accepted, or you ask for changes |
| Build loop | step 5 | gate FAIL, or a CONFIRMED high/med finding |
| Escape hatch | diagnose → step 5 | same failure twice, a hang, or an unclear root cause |
