# Vestibule — workflow diagram

Visual companion to [`WORKFLOW.md`](WORKFLOW.md). Box colors: amber = Fable (the main session),
purple = Opus, green = Haiku, blue = you (Victor).

## Flowchart (renders on GitHub / VS Code preview)

```mermaid
flowchart TD
    YOU([You: task / idea]):::you --> CTX

    subgraph MAIN["MAIN SESSION — Fable 5.2 · high (architect: plans, weighs reviews, writes docs)"]
        CTX[1. Read context<br/>CLAUDE.md · docs/plans · HISTORY]
        PLAN[2. Write plan<br/>docs/plans/&lt;milestone&gt;/&lt;step&gt;.md<br/>files · approach · edge cases]
        WEIGH[Address each plan finding<br/>accept · amend · reject + reason]
        READ[Read the diff against<br/>the plan + KARPATHY_EXAMPLES]
        DOCS[9. Update README/docs<br/>+ HISTORY bullet + BUILD_LOG entry]
    end

    CTX <-. "where is X?" .-> SCOUT[scout · Haiku · low<br/>file:line only]:::haiku

    CTX --> PLAN
    PLAN --> PGATE{Milestone or<br/>step plan?}
    PGATE -- yes --> SKEPTIC[3. skeptic · Opus · xhigh<br/>fresh context, read-only<br/>is this the right design?]:::opus
    SKEPTIC -- findings --> WEIGH
    WEIGH -- plan revised --> SIGN
    PGATE -- no --> SIGN{{4. You sign off<br/>logged in HISTORY}}:::you
    SIGN -- changes requested --> PLAN
    SIGN -- approved --> BUILD

    BUILD[5. builder · Opus · xhigh<br/>code + tests from the signed plan<br/>tests first · mutants]:::opus
    BUILD -- "plan wrong or silent:<br/>stops and reports" --> PLAN
    BUILD -- report + diff --> READ
    READ --> GATE[6. gate · Haiku · low<br/>ruff · mypy · pytest]:::haiku
    GATE -- FAIL --> BUILD
    GATE -- PASS --> SRC{Touches src/?}
    SRC -- no --> DOCS
    SRC -- yes --> CRITIC[7. critic · Opus · high<br/>fresh context, read-only<br/>assume it's broken]:::opus
    CRITIC -- "no issues found" --> DOCS
    CRITIC -- findings --> VERIFIER[8. verifier · Opus · high<br/>trace + repro each finding<br/>CONFIRMED · PLAUSIBLE · REFUTED]:::opus
    VERIFIER -- "confirmed high/med<br/>(same builder fixes)" --> BUILD
    VERIFIER -- "all refuted / low<br/>(reported to you)" --> DOCS

    DOCS --> DONE([Staging list + commit message]):::you
    DONE --> COMMIT([You commit]):::you

    BUILD -. "same failure twice /<br/>hang / unclear cause" .-> DIAG[diagnose · Opus · xhigh<br/>fresh-context root cause]:::opus
    DIAG -. "root cause + fix direction" .-> BUILD

    classDef you fill:#1e3a5f,stroke:#5b9bd5,color:#fff
    classDef opus fill:#3d2a5c,stroke:#a77bdb,color:#fff
    classDef haiku fill:#1f4d2e,stroke:#5cb85c,color:#fff
    style MAIN fill:#4d3a14,stroke:#e0a83a,color:#fff
```

## Plain-text version (for the terminal)

```
                         you: task / idea
                                │
                                ▼
 ┌──────────────── MAIN SESSION · Fable 5.2 ───────────────┐      ┌──────────────────┐
 │  1. read context (CLAUDE.md, plans, HISTORY) ◄───────────┼─────►│ scout · Haiku    │
 │                     │                                    │      │ file:line only   │
 │                     ▼                                    │      └──────────────────┘
 │  2. write plan → docs/plans/<milestone>/<step>.md        │
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
        5. builder · Opus: code + tests from the signed plan ◄──┐ ◄─ stuck? ─► diagnose · Opus
                       │  (plan wrong or silent → stops,        │            (root cause only)
                       │   reports, back to 2)                  │
                       ▼                                        │
           main session reads the diff against the plan         │
                       ▼                                        │
        6. gate · Haiku: ruff · mypy · pytest ──FAIL────────────┤
                       │ PASS                                   │
                       ▼                                        │
        touches src/? ──no──────────────────┐                   │
                       │ yes                │                   │
                       ▼                    │                   │
        7. critic · Opus (read-only)        │                   │
                       │ findings           │                   │
                       ▼                    │                   │
        8. verifier · Opus ─CONFIRMED high/med (same builder)───┘
                       │ refuted / low      │
                       ▼                    ▼
 ┌──────────────── MAIN SESSION · Fable 5.2 ───────────────┐
 │  9. README/docs + HISTORY + BUILD_LOG                    │
 └─────────────────────┬────────────────────────────────────┘
                       ▼
        staging list + commit message  ──►  YOU COMMIT
```

## Who runs on what

```
 Fable 5.2  ███     main session: plans, runs the loop, weighs findings, writes the docs
 Opus 5.5   ██████  builder (the code), skeptic, critic, verifier, diagnose
 Haiku 5.5  ██      scout, gate                         ← mechanical, cheap
 You        ██      plan sign-off, final say on rejected findings, git commit
```

## Effort per role

```
 xhigh  ████████  builder · skeptic · diagnose          ← code, design review, root cause: wrong = expensive
 high   ██████    main session · critic · verifier      ← plan and judge; find + prove defects in a bounded diff
 low    ██        scout · gate                          ← search + run commands, no judgment
```

## Finding → action

| Source | Verdict | Action |
|---|---|---|
| skeptic (plan) | design argument | main session accepts, amends, or rejects with a reason, before your sign-off |
| critic (diff) | CONFIRMED high/med | the builder fixes it, then repeat from step 6 (re-run critic if the fix spread) |
| critic (diff) | PLAUSIBLE | fix, or explain to you why not |
| critic (diff) | REFUTED / low | listed in the summary to you, no code change |

## Loop exits at a glance

| Loop | Goes back to | Trigger |
|---|---|---|
| Plan loop | step 2 | skeptic findings accepted, you ask for changes, or the builder finds the plan wrong or silent |
| Build loop | step 5 | gate FAIL, or a CONFIRMED high/med finding |
| Escape hatch | diagnose → step 5 | same failure twice, a hang, or an unclear root cause |
