# Codex Adversarial Review

Target: working tree diff
Verdict: needs-attention

No-ship: the plan still relies on a one-mode local validation for a store-specific digest risk, and the code diff regresses honest isolation reporting.

Findings:
- [high] Digest proof gate only validates the current image-store mode (docs/plans/M1-step6-pinning.md:89-92)
  The plan correctly identifies that Docker Desktop classic vs containerd image stores can resolve manifest-list refs differently, but the definition of proof only records the Docker version and image-store mode that happened to be used. That leaves the shipped defaults validated for one local configuration while still claiming support for other Docker Desktop store modes. Under the plan's own failure model, users on the untested supported store can follow the exact digest-pinned setup command and still fail preflight or `--pull never` runs.
  Recommendation: Make the gate cover every supported Docker Desktop image-store mode, or explicitly narrow support to the proven mode and make README/Blocked messages say how to switch or re-pull. Do not ship pins based only on one recorded local mode.
- [medium] Spawn failure reports invalid isolation state (src/vestibule/backends/naive.py:66-70)
  When subprocess creation raises FileNotFoundError, NaiveBackend now returns `isolation="false"`. The RunResult contract lists `none`, `container`, `container-degraded`, `namespaces-only`, and `native`, and selection/server logic keys safety behavior off exact isolation strings. A PATH race or removed interpreter would now render/log a bogus isolation level instead of the honest no-isolation state, weakening recovery and observability for a degraded dependency path.
  Recommendation: Restore `isolation="none"` for this FileNotFoundError path and add a regression test that forces `create_subprocess_exec` to raise FileNotFoundError and asserts the formatted result reports `isolation: none`.

Next steps:
- Attempted to create `docs/reviews/M1-step6-adversarial-review.md`, but the workspace is read-only and approval is disabled, so the write was blocked.
