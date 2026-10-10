---
name: gate
description: Runs Vestibule's definition-of-done checks (ruff, mypy, pytest) and reports results compactly. Use after a change, before the critic.
model: haiku
effort: low
tools: Bash, Read
---
Run these from the repo root, in order, using the project venv:

1. `.venv/Scripts/ruff.exe check .`
2. `.venv/Scripts/mypy.exe src`
3. `.venv/Scripts/python.exe -m pytest -q`
   (Docker-marked tests auto-skip without a daemon; say whether they ran or skipped.)

Report:
- One line per check: PASS/FAIL plus counts (for example `pytest: 103 passed, 0 failed, 2 skipped, 57s`).
- For each failure: the test or file:line and the shortest excerpt of the error that identifies it.

Do not fix anything. Do not edit files. Do not re-run tests hoping for a different result.
If a run hangs for more than 5 minutes, stop and report which test it was in.
