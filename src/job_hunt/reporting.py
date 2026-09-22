"""Deterministic reports rendered only from saved, validated records."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .models import (
    CollectionAttempt,
    CoverageSummary,
    Disposition,
    ReportJobRecord,
    ReportSnapshot,
)

DISPOSITION_ORDER = tuple(Disposition)
_TITLES = {
    Disposition.SHORTLISTED: "Shortlisted",
    Disposition.NEEDS_REVIEW: "Needs review",
    Disposition.BELOW_THRESHOLD: "Below threshold",
    Disposition.EXCLUDED: "Excluded",
    Disposition.UNASSESSED: "Unassessed",
}


def build_snapshot(
    run_id: str,
    as_of: date,
    scope: str,
    jobs: Iterable[ReportJobRecord],
    *,
    attempts: Iterable[CollectionAttempt] = (),
    source_total: int | None = None,
    scope_complete: bool | None = None,
) -> ReportSnapshot:
    """Reconcile one final disposition per saved validated job."""

    records = list(jobs)
    counts = {item: 0 for item in Disposition}
    for record in records:
        counts[record.disposition] += 1
    coverage = CoverageSummary(
        scope=scope,
        source_total=source_total,
        scope_complete=scope_complete,
        unique_jobs=len(records),
        final_dispositions=counts,
        attempts=list(attempts),
    )
    return ReportSnapshot(
        run_id=run_id,
        as_of=as_of,
        scope=scope,
        jobs=records,
        coverage=coverage,
    )


def sorted_jobs(snapshot: ReportSnapshot) -> dict[Disposition, list[ReportJobRecord]]:
    """Group final dispositions; shortlisted jobs get the required stable ordering."""

    grouped = {item: [] for item in DISPOSITION_ORDER}
    for job in snapshot.jobs:
        grouped[job.disposition].append(job)
    for disposition, jobs in grouped.items():
        jobs.sort(key=_shortlist_key if disposition == Disposition.SHORTLISTED else _job_id)
    return grouped


def render_json(snapshot: ReportSnapshot) -> str:
    grouped = sorted_jobs(snapshot)
    payload = {
        "schema_version": snapshot.schema_version,
        "run_id": snapshot.run_id,
        "as_of": snapshot.as_of.isoformat(),
        "scope": snapshot.scope,
        "coverage": snapshot.coverage.model_dump(mode="json"),
        "jobs": {
            disposition.value: [job.model_dump(mode="json") for job in grouped[disposition]]
            for disposition in DISPOSITION_ORDER
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_csv(snapshot: ReportSnapshot) -> str:
    stream = io.StringIO(newline="")
    fields = list(_row(snapshot, None))
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    wrote_job = False
    for jobs in sorted_jobs(snapshot).values():
        for job in jobs:
            writer.writerow({key: _spreadsheet_safe(value) for key, value in _row(snapshot, job).items()})
            wrote_job = True
    if not wrote_job:
        writer.writerow({key: _spreadsheet_safe(value) for key, value in _row(snapshot, None).items()})
    return stream.getvalue()


def render_markdown(snapshot: ReportSnapshot) -> str:
    coverage = snapshot.coverage
    completion = (
        "yes"
        if coverage.scope_complete is True
        else "no" if coverage.scope_complete is False else "not established"
    )
    lines = [
        f"# Job report - {_markdown(snapshot.as_of.isoformat())}",
        "",
        "## Coverage",
        "",
        f"- Scope: {_markdown(snapshot.scope)}",
        f"- Unique saved jobs: {coverage.unique_jobs}",
        f"- Source total: {coverage.source_total if coverage.source_total is not None else 'unknown'}",
        f"- Complete within this scope: {completion}",
        f"- Attempts: {len(coverage.attempts)}",
        f"- Errors: {sum(attempt.error is not None for attempt in coverage.attempts)}",
        "- Final dispositions: "
        + ", ".join(
            f"{_TITLES[item].lower()}={coverage.final_dispositions[item]}"
            for item in DISPOSITION_ORDER
        ),
        "",
        "### Attempt history",
        "",
    ]
    if coverage.attempts:
        lines.extend(["| Attempt | Source | Scope | Time | Found | Source total | Error |", "|---|---|---|---|---:|---:|---|"])
        for attempt in coverage.attempts:
            lines.append(
                "| "
                + " | ".join(
                    _markdown(value)
                    for value in (
                        attempt.attempt_id,
                        attempt.source,
                        attempt.scope,
                        attempt.attempted_at.isoformat(),
                        str(len(set(attempt.discovered_job_ids))),
                        str(attempt.discovered_total) if attempt.discovered_total is not None else "unknown",
                        attempt.error or "",
                    )
                )
                + " |"
            )
    else:
        lines.append("No saved collection attempts.")

    for disposition, jobs in sorted_jobs(snapshot).items():
        lines.extend(["", f"## {_TITLES[disposition]}", ""])
        if not jobs:
            lines.append("None.")
            continue
        for record in jobs:
            job = record.job
            assessment = record.assessment
            lines.extend(
                [
                    f"### {_markdown(job.company)} - {_markdown(job.title)}",
                    "",
                    f"- Job ID: {_markdown(job.job_id)}",
                    f"- Run ID: {_markdown(record.run_id)}",
                    f"- Candidate/profile: {_markdown(record.candidate_id)} / {_markdown(record.profile_hash)}",
                    f"- Source: {_markdown(job.source or 'unknown')} ({_markdown(str(job.url) if job.url else 'no URL')})",
                    f"- Source records: {_markdown(', '.join(record.source_record_ids))}",
                    f"- Original posted date: {_markdown(_posted_at(record))} ({'verified' if record.original_date_verified else 'not verified'})",
                    f"- Availability: {_availability(record.available)}",
                    f"- Raw/rounded score: {_number(record.raw_score)} / {_number(record.rounded_score)}; threshold: {_number(record.threshold)}",
                    f"- Score components: {_markdown(_components(record))}",
                    f"- Eligibility/gates: {_markdown(record.eligibility)} / {_markdown(_json(record.gates))}",
                    f"- Assessment complete: {'yes' if assessment and assessment.complete else 'no'}",
                    f"- Reason: {_markdown(record.reason or 'not provided')}",
                    f"- Requirement evidence: {_markdown(_evidence(record))}",
                ]
            )
    return "\n".join(lines) + "\n"


def render_reports(snapshot: ReportSnapshot) -> dict[str, str]:
    return {
        "report.md": render_markdown(snapshot),
        "report.csv": render_csv(snapshot),
        "report.json": render_json(snapshot),
    }


def write_reports(snapshot: ReportSnapshot, output_dir: str | Path) -> dict[str, Path]:
    """Overwrite reproducible report views; the saved snapshot remains authoritative."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, content in render_reports(snapshot).items():
        path = output / name
        path.write_text(content, encoding="utf-8", newline="")
        paths[name] = path
    return paths


def regenerate_reports(saved_snapshot: str | Path, output_dir: str | Path) -> dict[str, Path]:
    """Regenerate views from a persisted snapshot, with no model or network dependency."""

    data = json.loads(Path(saved_snapshot).read_text(encoding="utf-8"))
    return write_reports(ReportSnapshot.model_validate(data), output_dir)


generate_reports = write_reports


def _shortlist_key(record: ReportJobRecord) -> tuple[float, bool, int, str]:
    posted = record.job.posted_at
    known = record.original_date_verified and posted is not None
    day = posted.date() if isinstance(posted, datetime) else posted
    return (
        -(record.raw_score if record.raw_score is not None else -1),
        not known,
        -(day.toordinal() if known else 0),
        record.job.job_id,
    )


def _job_id(record: ReportJobRecord) -> str:
    return record.job.job_id


def _row(snapshot: ReportSnapshot, record: ReportJobRecord | None) -> dict[str, object]:
    coverage = snapshot.coverage
    if record is None:
        row = {
            key: ""
            for key in (
                "run_id", "as_of", "candidate_id", "profile_hash", "job_id", "company", "title",
                "disposition", "reason", "raw_score", "rounded_score", "threshold", "score_requirements",
                "score_preferences", "score_company", "eligibility", "gates", "assessment_complete",
                "requirement_evidence", "source", "url", "source_record_ids", "original_posted_at",
                "original_date_verified", "availability", "coverage_scope", "coverage_source_total",
                "coverage_scope_complete", "coverage_unique_jobs", "coverage_final_dispositions",
                "coverage_attempt_count", "coverage_error_count", "coverage_attempts",
            )
        }
        row.update(
            {
                "run_id": snapshot.run_id,
                "as_of": snapshot.as_of.isoformat(),
                "coverage_scope": coverage.scope,
                "coverage_source_total": coverage.source_total if coverage.source_total is not None else "",
                "coverage_scope_complete": coverage.scope_complete if coverage.scope_complete is not None else "",
                "coverage_unique_jobs": coverage.unique_jobs,
                "coverage_final_dispositions": _json(coverage.final_dispositions),
                "coverage_attempt_count": len(coverage.attempts),
                "coverage_error_count": sum(attempt.error is not None for attempt in coverage.attempts),
                "coverage_attempts": _json(coverage.attempts),
            }
        )
        return row
    job = record.job
    assessment = record.assessment
    scores = assessment.scores if assessment else None
    return {
        "run_id": record.run_id,
        "as_of": snapshot.as_of.isoformat(),
        "candidate_id": record.candidate_id,
        "profile_hash": record.profile_hash,
        "job_id": job.job_id,
        "company": job.company,
        "title": job.title,
        "disposition": record.disposition.value,
        "reason": record.reason,
        "raw_score": _number(record.raw_score),
        "rounded_score": _number(record.rounded_score),
        "threshold": _number(record.threshold),
        "score_requirements": _number(scores.requirements if scores else None),
        "score_preferences": _number(scores.preferences if scores else None),
        "score_company": _number(scores.company if scores else None),
        "eligibility": record.eligibility,
        "gates": _json(record.gates),
        "assessment_complete": bool(assessment and assessment.complete),
        "requirement_evidence": _evidence(record),
        "source": job.source or "",
        "url": str(job.url) if job.url else "",
        "source_record_ids": _json(record.source_record_ids),
        "original_posted_at": _posted_at(record),
        "original_date_verified": record.original_date_verified,
        "availability": _availability(record.available),
        "coverage_scope": coverage.scope,
        "coverage_source_total": coverage.source_total if coverage.source_total is not None else "",
        "coverage_scope_complete": coverage.scope_complete if coverage.scope_complete is not None else "",
        "coverage_unique_jobs": coverage.unique_jobs,
        "coverage_final_dispositions": _json(coverage.final_dispositions),
        "coverage_attempt_count": len(coverage.attempts),
        "coverage_error_count": sum(attempt.error is not None for attempt in coverage.attempts),
        "coverage_attempts": _json(coverage.attempts),
    }


def _evidence(record: ReportJobRecord) -> str:
    if not record.assessment or not record.assessment.requirement_assessments:
        return "not evidenced"
    return _json(
        [
            {
                "requirement_id": item.requirement_id,
                "classification": item.classification.value.replace("_", " "),
                "evidence": [evidence.model_dump(mode="json") for evidence in item.evidence],
                "supported_portions": item.supported_portions,
                "missing_portions": item.missing_portions,
            }
            for item in record.assessment.requirement_assessments
        ]
    )


def _components(record: ReportJobRecord) -> str:
    return _json(record.assessment.scores) if record.assessment and record.assessment.scores else "not assessed"


def _posted_at(record: ReportJobRecord) -> str:
    return record.job.posted_at.isoformat() if record.job.posted_at else "unknown"


def _availability(value: bool | None) -> str:
    return "available" if value is True else "unavailable" if value is False else "unknown"


def _number(value: float | None) -> str:
    return "" if value is None else format(value, "g")


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value]
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _spreadsheet_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
