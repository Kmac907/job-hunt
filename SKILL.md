# job-hunt repository skill

This is a thin operator skill for the existing `job-hunt` CLI. Use the CLI as
the implementation: do not recreate collection, model calls, scoring,
percentage calculations, report rendering, or persistence here.

## Workflow

1. Run `job-hunt doctor --config config.yaml` and resolve configuration or
   authentication errors.
2. Run `job-hunt run --config config.yaml --jobs jobs.json` for a saved-fixture
   run, or omit `--jobs` when `collectors` is configured.
3. Stop for human review. Open the run's saved `profile-review.md`; it contains
   the extracted profile and evidence without raw resume text. Do not approve a
   profile that is inaccurate or unsupported.
4. Approve the exact version from the run manifest:
   `job-hunt profile approve --config config.yaml --run-id RUN_ID
   --profile-version PROFILE_VERSION`.
5. Continue only after approval:
   `job-hunt resume --config config.yaml --run-id RUN_ID`.
6. Render saved results with
   `job-hunt report --config config.yaml --run-id RUN_ID`. This reads the saved
   `report-snapshot.json`; it does not recollect jobs or invoke the model.

The skill may explain commands and surface saved paths, statuses, errors,
coverage, and review instructions. It must not submit applications, send messages,
install collectors, hide incomplete coverage, or recursively invoke itself. It
must not add its own model, network, collector, or scoring logic.

Scores are bounded planning signals, not hiring decisions or ATS equivalence.
Unknown, blocked, unsupported, and incomplete coverage must remain visible in
the rendered results. Resume and job data may be sent to the configured
external Codex service during inference; keep local artifacts private.
