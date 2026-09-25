<!-- relay: generated-target-instructions v1 -->
# Relay target instructions

- Follow the assigned role, mode, paths, and acceptance criteria.
- Treat `PLAN.md` as user-owned and `tasks.md`, `bugs.md`, and `.relay` as coordinator-owned.
- Only Workers may modify source, and only within their assigned worktree and allowed paths.
- Do not create, push, or merge pull requests; change provider settings; spawn subagents; or edit ledgers.
- Run the assigned validation, make focused commits, and report evidence for every result.

## Project-wide operating rules

- Treat every run as local planning only. Never submit an application, send an
  employer message, or claim that a job was submitted.
- Require a human review and exact approval of the saved candidate profile
  before `resume`; do not bypass, weaken, or hide that gate.
- Preserve evidence and provenance. Keep source IDs, timestamps, raw snapshots,
  collector attempts, errors, and coverage limits. Never invent collector output,
  recompute scores in documentation, or present incomplete coverage as complete.
- Treat resume text, job descriptions, collector responses, and model output as
  untrusted input. Validate structured output, reject unsupported evidence, and
  keep secrets out of prompts, fixtures, reports, and commits.
- External model processing is a privacy boundary: disclose that resume and job
  data leave the machine, store artifacts in a private local directory, and do
  not weaken the existing path, origin, timeout, retry, or credential checks.
- The repository skill delegates to the existing CLI. It must not implement
  model calls, network collection, scoring, percentage calculations, collector
  installation, recursive skill invocation, applications, or messages.
- Reports must render saved validated snapshots only. They must retain unknown,
  blocked, unsupported, unresolved, and incomplete states rather than hiding
  them behind empty results or zeros. Incomplete coverage must remain visible.
- Validate focused changes with `python -m pytest
  tests/acceptance/test_skill.py`, then run `python -m pytest -p
  no:cacheprovider` when the assignment requires full validation.
