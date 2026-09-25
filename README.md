# job-hunt

Local-first job-search planning. The program collects a bounded set of public
job records, extracts a reviewable candidate profile, assesses saved records,
and writes reproducible reports. It does not apply for jobs or contact
employers.

## Setup

Use Python 3.11 or newer, then install the package and test extras:

```sh
python -m pip install -e ".[test]"
cp config.example.yaml config.yaml
cp companies.example.yaml companies.yaml
```

Put the resume at the configured `resume_path`. Keep `config.yaml`, the
resume, `companies.yaml`, and `output_dir` private; they can contain personal
data and job descriptions. The output is local filesystem storage, not
encryption or access control. Apply normal OS permissions and do not commit
these files.

## Configuration and commands

`config.yaml` is YAML with schema version `1.0`. Set `resume_path`,
`companies_path`, `output_dir`, `as_of`, `timezone`, `match_threshold`,
`scoring_weights`, `preferences`, and `runtime`. `runtime.model` may instead
be supplied by `CODEX_MODEL`. Set `collectors: []` for an offline saved-fixture
run, or configure a supported collector and its allowed HTTPS origins. Secret
values belong in environment variables, not YAML.

Run the preflight check before a run:

```sh
job-hunt doctor --config config.yaml
```

Offline workflow (the fixture is a saved JSON file):

```sh
job-hunt run --config config.yaml --jobs jobs.json
job-hunt profile approve --config config.yaml --run-id RUN_ID --profile-version PROFILE_VERSION
job-hunt resume --config config.yaml --run-id RUN_ID
job-hunt report --config config.yaml --run-id RUN_ID
```

The first command pauses after writing `profile-review.md`. Review that file,
then approve the exact `profile_version` printed by `run` (or recorded in the
run's `manifest.json`). `resume` requires that approval and that the selected
resume has not changed. `report` only regenerates `report.md`, `report.csv`,
and `report.json` from the saved `report-snapshot.json`; it does not recollect
jobs or call the model.

## Processing, evidence, and privacy

When inference runs, the existing Codex adapter invokes the configured
external Codex model. Resume content and job descriptions are therefore sent
to that external service; `doctor` prints the same disclosure. The adapter
requires structured responses and source-grounded evidence. Do not put secrets
or unrelated personal data in inputs.

The profile review is a gate, not a form submission. Correct the source resume
and start a new run if the extracted profile is wrong. Reports preserve
evidence, requirement classifications, availability, source IDs, and saved
collection attempts. Unverified or unresolved evidence remains unassessed; it
is not silently converted into a zero or a match.

Scores are planning signals from this configuration and this bounded run.
They are not a hiring decision, a guarantee of eligibility, or an employer
ATS result. The evaluation report is limited to its supplied labeled set and
does not establish ATS equivalence.

Coverage is scope-bounded. `source_total`, `unique_jobs`, attempt history, and
`scope_complete` describe only the configured companies, query, date, and
limits. `scope_complete: false` or `unknown`, an error, a limit, an
unsupported adapter, or unresolved verification means coverage is incomplete;
the reports retain that fact. An empty result is not evidence that an entire
employer site has no jobs.

Incomplete coverage is an operator-visible result, not a reason to fill in
missing jobs or scores.

This project has no scheduler and no employer-side action boundary. A cron,
Task Scheduler, or other external orchestrator may run the documented CLI,
but it must preserve the same review gate, private storage, and incomplete
coverage. Scheduling does not authorize applications, messages, or any other
external action.

## Collector support matrix

This matrix reflects the recorded current live outcomes in
`tests/fixtures/collectors/`; it is not a claim of universal portal support.

| Adapter | Current status | Evidence or exact blocker |
|---|---|---|
| Ashby | Supported | Recorded live success: official public-board probe returned HTTP 200 and 65 jobs. |
| Amazon | Unsupported | The search response exposed no canonical `/en/jobs/<id>` detail link; the response exposed no machine-readable pagination or query-completion link; availability remains unknown because HTTP success is not availability evidence. |
| Greenhouse | Unverified | Live support has not been verified; the recorded fixture says to run `pytest -m live tests/acceptance/test_greenhouse_live.py`. |
| Lever | Unsupported | The recorded live contract check found `RuntimeError: Lever detail omitted a recognized posting state`. |
| Microsoft | Unsupported | Microsoft's official careers entry point redirects to the new `apply.careers.microsoft.com/careers` experience; no public discovery, detail, pagination, closure, or original-posting-date contract was verified within the inspection deadline. |

Only Ashby has a recorded current live success. Unsupported and unverified
adapters remain explicit outcomes; they are not silently treated as complete.
Do not add a collector to this table without a new recorded live result.

## Troubleshooting and validation

- `doctor` reports an unset model: set `runtime.model` or `CODEX_MODEL`, and
  authenticate the external Codex CLI.
- A run is `awaiting_profile_review`: inspect the saved
  `profile-review.md`, then use `profile approve` with the exact version.
- Approval fails because the resume changed: start a new run.
- A collector is unsupported, blocked, or incomplete: inspect the run's
  `coverage.json` and attempt history; do not infer missing jobs.
- Reports need refreshing: use `report` with the saved run ID. It reads the
  saved snapshot and does not need the resume, collector, or model.

Run the focused and full validations:

```sh
python -m pytest tests/acceptance/test_skill.py
python -m pytest -p no:cacheprovider
```

Live probes are bounded evidence checks and are separate from the normal
offline validation:

```sh
python -m pytest -m live tests/acceptance/test_greenhouse_live.py
```
