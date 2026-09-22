"""Deterministic, review-gated CV extraction and profile evidence helpers."""

from __future__ import annotations

import json
import re
import zlib
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field

from .models import CandidateProfile, ProfileEvidenceKind, SupportedInterval


class ExtractionReviewError(ValueError):
    """An extraction failure that requires a human to correct or replace the CV."""

    def __init__(self, *issues: str) -> None:
        self.issues = tuple(issues)
        super().__init__("CV extraction needs review: " + "; ".join(self.issues))


class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    kind: Literal["paragraph", "table_cell", "page", "line_range"]
    location: str
    text: str


class CVExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_name: str
    source_format: Literal["docx", "pdf", "text"]
    source_hash: str
    extraction_hash: str
    normalized_text: str
    blocks: tuple[SourceBlock, ...]
    review_issues: tuple[str, ...] = ()

    def resolve_source(self, source_id: str) -> SourceBlock:
        block = next((item for item in self.blocks if item.source_id == source_id), None)
        if block is None:
            raise ValueError(f"unresolved CV source ID {source_id!r}")
        return block

    def resolve_quote(self, source_id: str, quote: str) -> SourceBlock:
        block = self.resolve_source(source_id)
        if not quote or quote not in block.text:
            raise ValueError(f"quote is not present in CV source {source_id!r}")
        return block


class ProfileApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str
    cv_hash: str
    extraction_hash: str
    profile_hash: str


class DurationBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    minimum_days: int = Field(ge=0)
    maximum_days: int = Field(ge=0)
    supported_intervals: int = Field(ge=0)
    capability: str | None = None


def _normalized(value: str) -> str:
    return "\n".join(
        " ".join(line.replace("\xa0", " ").split())
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


def _blocks_hash(blocks: Iterable[SourceBlock]) -> str:
    payload = [block.model_dump(mode="json") for block in blocks]
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _snapshot(
    path: Path,
    source_format: Literal["docx", "pdf", "text"],
    raw: bytes,
    blocks: list[SourceBlock],
    required_sections: Iterable[str],
) -> CVExtraction:
    normalized = "\n".join(block.text for block in blocks if block.text)
    issues = []
    if not normalized:
        issues.append("no readable text was found; provide a text-based CV instead of an image-only file")
    for section in required_sections:
        if section.casefold() not in normalized.casefold():
            issues.append(f"required section {section!r} was not found")
    if "\ufffd" in normalized or any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        issues.append("text decoding or reading order is unreliable; review the extracted text")
    result = CVExtraction(
        source_name=path.name,
        source_format=source_format,
        source_hash=sha256(raw).hexdigest(),
        extraction_hash=_blocks_hash(blocks),
        normalized_text=normalized,
        blocks=tuple(blocks),
        review_issues=tuple(issues),
    )
    if issues:
        raise ExtractionReviewError(*issues)
    return result


def extract_cv(path: str | Path, *, required_sections: Iterable[str] = ()) -> CVExtraction:
    """Extract a reviewable snapshot from DOCX, text PDF, or UTF-8 plain text."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ExtractionReviewError(f"cannot read {source}: {exc}") from exc
    suffix = source.suffix.casefold()
    try:
        if suffix == ".docx":
            blocks = _docx_blocks(raw)
            source_format: Literal["docx", "pdf", "text"] = "docx"
        elif suffix == ".pdf":
            blocks = _pdf_blocks(raw)
            source_format = "pdf"
        elif suffix in {".txt", ".text", ".md"}:
            blocks = _text_blocks(raw)
            source_format = "text"
        else:
            raise ExtractionReviewError("unsupported CV format; use DOCX, text-based PDF, or UTF-8 text")
    except ExtractionReviewError:
        raise
    except (BadZipFile, ElementTree.ParseError, UnicodeError, ValueError, zlib.error) as exc:
        raise ExtractionReviewError(f"malformed or truncated {suffix.removeprefix('.').upper()} file: {exc}") from exc
    return _snapshot(source, source_format, raw, blocks, required_sections)


def _text_blocks(raw: bytes) -> list[SourceBlock]:
    lines = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[SourceBlock] = []
    start = 0
    for index in range(len(lines) + 1):
        if index < len(lines) and lines[index].strip():
            start = start or index + 1
            continue
        if start:
            end = index
            text = _normalized("\n".join(lines[start - 1 : end]))
            blocks.append(
                SourceBlock(
                    source_id=f"text:lines:{start}-{end}",
                    kind="line_range",
                    location=f"lines {start}-{end}",
                    text=text,
                )
            )
            start = 0
    return blocks


def _docx_blocks(raw: bytes) -> list[SourceBlock]:
    from io import BytesIO

    with ZipFile(BytesIO(raw)) as archive:
        try:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        except KeyError as exc:
            raise ValueError("word/document.xml is missing") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    if any(node.tag.rsplit("}", 1)[-1] in {"altChunk", "textbox", "txbxContent"} for node in root.iter()):
        raise ExtractionReviewError(
            "DOCX reading order is unreliable because it contains linked or floating text; save a linearized copy"
        )

    def paragraph_text(node: ElementTree.Element) -> str:
        parts: list[str] = []
        for value in node.iter():
            name = value.tag.rsplit("}", 1)[-1]
            if name == "t":
                parts.append(value.text or "")
            elif name == "tab":
                parts.append("\t")
            elif name in {"br", "cr"}:
                parts.append("\n")
        return _normalized("".join(parts))

    body = root.find(f"{ns}body")
    if body is None:
        raise ValueError("document body is missing")
    blocks: list[SourceBlock] = []
    paragraph = table = 0
    for child in body:
        if child.tag == f"{ns}p":
            paragraph += 1
            text = paragraph_text(child)
            if text:
                blocks.append(
                    SourceBlock(
                        source_id=f"docx:paragraph:{paragraph}",
                        kind="paragraph",
                        location=f"paragraph {paragraph}",
                        text=text,
                    )
                )
        elif child.tag == f"{ns}tbl":
            table += 1
            for row_number, row in enumerate(child.findall(f"{ns}tr"), 1):
                for cell_number, cell in enumerate(row.findall(f"{ns}tc"), 1):
                    text = _normalized(
                        "\n".join(
                            value
                            for item in cell.findall(f".//{ns}p")
                            if (value := paragraph_text(item))
                        )
                    )
                    if text:
                        source_id = f"docx:table:{table}:row:{row_number}:cell:{cell_number}"
                        blocks.append(
                            SourceBlock(
                                source_id=source_id,
                                kind="table_cell",
                                location=source_id.removeprefix("docx:").replace(":", " "),
                                text=text,
                            )
                        )
    return blocks


_PDF_OBJECT = re.compile(rb"(?m)^\s*(\d+)\s+\d+\s+obj\b(.*?)\bendobj\b", re.DOTALL)
_PDF_REF = re.compile(rb"(\d+)\s+\d+\s+R")
_PDF_STRING = rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]+>"
_PDF_SHOW = re.compile(rb"(" + _PDF_STRING + rb")\s*(?:Tj|'|\")|\[(.*?)\]\s*TJ", re.DOTALL)


def _pdf_blocks(raw: bytes) -> list[SourceBlock]:
    if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
        raise ValueError("PDF header or end marker is missing")
    objects = {int(match.group(1)): match.group(2) for match in _PDF_OBJECT.finditer(raw)}
    page_objects = {number for number, value in objects.items() if re.search(rb"/Type\s*/Page\b", value)}
    if not page_objects:
        raise ValueError("PDF contains no readable pages")
    page_numbers = _pdf_page_order(objects, page_objects)
    blocks: list[SourceBlock] = []
    for page_number, object_number in enumerate(page_numbers, 1):
        page = objects[object_number]
        contents = re.search(rb"/Contents\s*(\[[^]]*\]|\d+\s+\d+\s+R)", page, re.DOTALL)
        streams = []
        if contents:
            for reference in _PDF_REF.findall(contents.group(1)):
                stream_object = objects.get(int(reference), b"")
                stream_match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", stream_object, re.DOTALL)
                if not stream_match:
                    continue
                stream = stream_match.group(1)
                if b"/FlateDecode" in stream_object[: stream_match.start()]:
                    stream = zlib.decompress(stream)
                streams.append(stream)
        text = _normalized(" ".join(_pdf_text(stream) for stream in streams))
        if not text:
            raise ExtractionReviewError(
                f"PDF page {page_number} has no readable text and may be image-only; provide a text-based PDF"
            )
        blocks.append(
            SourceBlock(
                source_id=f"pdf:page:{page_number}",
                kind="page",
                location=f"page {page_number}",
                text=text,
            )
        )
    return blocks


def _pdf_page_order(objects: dict[int, bytes], pages: set[int]) -> list[int]:
    catalogs = [value for value in objects.values() if re.search(rb"/Type\s*/Catalog\b", value)]
    root_match = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", catalogs[0]) if len(catalogs) == 1 else None
    if root_match:
        ordered: list[int] = []
        active: set[int] = set()

        def visit(number: int) -> None:
            if number in active or number not in objects:
                raise ExtractionReviewError("PDF page order is unreliable; export a new text-based PDF")
            if number in pages:
                ordered.append(number)
                return
            active.add(number)
            kids = re.search(rb"/Kids\s*\[(.*?)\]", objects[number], re.DOTALL)
            if not kids:
                raise ExtractionReviewError("PDF page order is unreliable; export a new text-based PDF")
            for reference in _PDF_REF.findall(kids.group(1)):
                visit(int(reference))
            active.remove(number)

        visit(int(root_match.group(1)))
        if len(ordered) != len(pages) or set(ordered) != pages:
            raise ExtractionReviewError("PDF page order is unreliable; export a new text-based PDF")
        return ordered
    if len(pages) > 1:
        raise ExtractionReviewError("PDF page order is unreliable; export a new text-based PDF")
    return list(pages)


def _pdf_text(stream: bytes) -> str:
    values: list[str] = []
    for match in _PDF_SHOW.finditer(stream):
        if match.group(1):
            values.append(_decode_pdf_string(match.group(1)))
        else:
            values.append("".join(_decode_pdf_string(item) for item in re.findall(_PDF_STRING, match.group(2) or b"")))
    return " ".join(value for value in values if value)


def _decode_pdf_string(token: bytes) -> str:
    if token.startswith(b"<"):
        value = bytes.fromhex(re.sub(rb"\s", b"", token[1:-1]).decode("ascii"))
    else:
        source = token[1:-1]
        output = bytearray()
        index = 0
        escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        while index < len(source):
            if source[index] != 92:
                output.append(source[index])
                index += 1
                continue
            index += 1
            if index == len(source):
                break
            if 48 <= source[index] <= 55:
                end = index
                while end < min(index + 3, len(source)) and 48 <= source[end] <= 55:
                    end += 1
                output.append(int(source[index:end], 8))
                index = end
            else:
                output.append(escapes.get(source[index], source[index]))
                index += 1
        value = bytes(output)
    if value.startswith((b"\xfe\xff", b"\xff\xfe")):
        return value.decode("utf-16")
    return value.decode("cp1252")


def profile_hash(profile: CandidateProfile) -> str:
    payload = profile.model_dump(mode="json")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def approve_profile(extraction: CVExtraction, profile: CandidateProfile) -> ProfileApproval:
    """Approve only a fully grounded profile tied to this exact extraction."""

    if extraction.review_issues:
        raise ExtractionReviewError(*extraction.review_issues)
    if not extraction.blocks or not extraction.normalized_text:
        raise ExtractionReviewError("no readable text was found; provide a text-based CV")
    expected_text = "\n".join(block.text for block in extraction.blocks if block.text)
    source_ids = [block.source_id for block in extraction.blocks]
    if (
        extraction.normalized_text != expected_text
        or extraction.extraction_hash != _blocks_hash(extraction.blocks)
        or len(source_ids) != len(set(source_ids))
    ):
        raise ExtractionReviewError("extraction snapshot integrity failed; extract the CV again")
    if profile.cv_hash != extraction.source_hash or profile.extraction_hash != extraction.extraction_hash:
        raise ValueError("profile does not belong to this exact CV extraction")
    if profile.resume_text != extraction.normalized_text:
        raise ValueError("profile text does not match the approved extraction snapshot")
    for item in profile.evidence:
        for source_id in item.source_ids:
            extraction.resolve_source(source_id)
            if item.matching_credit:
                extraction.resolve_quote(source_id, item.wording)
    for interval in profile.experience_intervals:
        for source_id in interval.source_ids:
            extraction.resolve_source(source_id)
    for qualification in profile.qualifications:
        for source_id in qualification.source_ids:
            extraction.resolve_quote(source_id, qualification.original_title)
    listed_skills = {
        _normalized(item.wording).casefold()
        for item in profile.evidence
        if item.kind == ProfileEvidenceKind.LISTED_SKILL
    }
    for skill in profile.skills:
        normalized_skill = _normalized(skill).casefold()
        if not normalized_skill or not any(
            re.search(rf"(?<!\w){re.escape(normalized_skill)}(?!\w)", wording)
            for wording in listed_skills
        ):
            raise ValueError(f"listed skill {skill!r} lacks listed-skill evidence")
    return ProfileApproval(
        candidate_id=profile.candidate_id,
        cv_hash=extraction.source_hash,
        extraction_hash=extraction.extraction_hash,
        profile_hash=profile_hash(profile),
    )


def approval_is_valid(approval: ProfileApproval, extraction: CVExtraction, profile: CandidateProfile) -> bool:
    try:
        return approve_profile(extraction, profile) == approval
    except (ExtractionReviewError, ValueError):
        return False


def merge_profiles(left: CandidateProfile, right: CandidateProfile) -> CandidateProfile:
    if (
        not left.cv_hash
        or not left.extraction_hash
        or (left.cv_hash, left.extraction_hash, left.resume_text)
        != (right.cv_hash, right.extraction_hash, right.resume_text)
    ):
        raise ValueError("profiles from different CV extractions cannot be merged")
    if left.candidate_id != right.candidate_id:
        raise ValueError("profiles for different candidates cannot be merged")

    def unique(first: list, second: list) -> list:
        return list(
            dict.fromkeys(
                json.dumps(
                    item.model_dump(mode="json") if isinstance(item, BaseModel) else item,
                    sort_keys=True,
                )
                for item in first + second
            )
        )

    # Convert the compact deduplication keys back through Pydantic's existing field types.
    data = left.model_dump(mode="json")
    for field in ("evidence", "experience_intervals", "qualifications"):
        data[field] = [
            json.loads(item)
            for item in unique(getattr(left, field), getattr(right, field))
        ]
    data["skills"] = list(dict.fromkeys(left.skills + right.skills))
    return CandidateProfile.model_validate(data)


def _merged_days(intervals: list[tuple[date, date]]) -> int:
    total = 0
    end: date | None = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).days + 1
            end = stop
        elif stop > end:
            total += (stop - end).days
            end = stop
    return total


def supported_duration(
    intervals: Iterable[SupportedInterval],
    *,
    as_of: date,
    capability: str | None = None,
) -> DurationBounds:
    """Return merged evidence-backed duration; capability duration is never overall tenure."""

    selected = [
        item
        for item in intervals
        if capability is None or item.capability == capability
    ]
    minimum: list[tuple[date, date]] = []
    maximum: list[tuple[date, date]] = []
    for item in selected:
        start_early, start_late = item.start.value, item.start.latest
        if item.present:
            end_early = end_late = as_of
        else:
            assert item.end is not None
            end_early, end_late = item.end.value, item.end.latest
        if end_late < start_early:
            raise ValueError("interval cannot begin after its supported end or the run as-of date")
        if start_late < end_early:
            minimum.append((start_late, end_early))
        else:
            possible_same_day = max(start_early, end_early)
            minimum.append((possible_same_day, possible_same_day))
        maximum.append((start_early, end_late))
    return DurationBounds(
        minimum_days=_merged_days(minimum),
        maximum_days=_merged_days(maximum),
        supported_intervals=len(selected),
        capability=capability,
    )


# Friendly aliases for callers that use the artifact terminology.
extract_cv_snapshot = extract_cv
validate_approval = approval_is_valid
