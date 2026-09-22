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

from .models import CandidateProfile, ProfileDate, ProfileEvidenceKind, SupportedInterval


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
    lines = normalized.splitlines()
    for section in required_sections:
        heading = re.compile(rf"^(?:#+\s*)?{re.escape(section)}\s*(?::.*)?$", re.IGNORECASE)
        if not any(heading.fullmatch(line) for line in lines):
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
    def visit(child: ElementTree.Element) -> None:
        nonlocal paragraph, table
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
        elif child.tag == f"{ns}sdt":
            content = child.find(f"{ns}sdtContent")
            if content is not None:
                for item in content:
                    visit(item)

    for child in body:
        visit(child)
    return blocks


_PDF_OBJECT = re.compile(rb"(?m)^\s*(\d+)\s+\d+\s+obj\b(.*?)\bendobj\b", re.DOTALL)
_PDF_REF = re.compile(rb"(\d+)\s+\d+\s+R")
_PDF_STRING = rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]+>"
_PDF_NUMBER = rb"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_PDF_EVENT = re.compile(
    rb"/(?P<font>[^\s/<>()\[\]]+)\s+" + _PDF_NUMBER + rb"\s+Tf"
    rb"|(?P<single>" + _PDF_STRING + rb")\s*(?:Tj|'|\")"
    rb"|\[(?P<array>.*?)\]\s*TJ"
    rb"|(?P<tdx>" + _PDF_NUMBER + rb")\s+(?P<tdy>" + _PDF_NUMBER + rb")\s+T[Dd]"
    rb"|(?:" + _PDF_NUMBER + rb"\s+){4}(?P<tmx>" + _PDF_NUMBER + rb")\s+(?P<tmy>" + _PDF_NUMBER + rb")\s+Tm",
    re.DOTALL,
)


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
        fonts = _pdf_fonts(page, objects)
        contents = re.search(rb"/Contents\s*(\[[^]]*\]|\d+\s+\d+\s+R)", page, re.DOTALL)
        streams = []
        if contents:
            for reference in _PDF_REF.findall(contents.group(1)):
                stream_object = objects.get(int(reference), b"")
                stream_match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", stream_object, re.DOTALL)
                if not stream_match:
                    continue
                streams.append(_pdf_stream(stream_object, stream_match))
        text = _normalized(" ".join(_pdf_text(stream, fonts) for stream in streams))
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


def _pdf_stream(value: bytes, match: re.Match[bytes] | None = None) -> bytes:
    match = match or re.search(rb"stream\r?\n(.*?)\r?\nendstream", value, re.DOTALL)
    if not match:
        raise ValueError("referenced PDF stream is missing")
    stream = match.group(1)
    return zlib.decompress(stream) if b"/FlateDecode" in value[: match.start()] else stream


def _pdf_fonts(page: bytes, objects: dict[int, bytes]) -> dict[bytes, tuple[dict[bytes, str] | None, str]]:
    resource = page
    seen: set[int] = set()
    while not re.search(rb"/Resources\b", resource):
        parent = re.search(rb"/Parent\s+(\d+)\s+\d+\s+R", resource)
        if not parent or int(parent.group(1)) in seen:
            return {}
        number = int(parent.group(1))
        seen.add(number)
        resource = objects.get(number, b"")
    reference = re.search(rb"/Resources\s+(\d+)\s+\d+\s+R", resource)
    if reference:
        resource = objects.get(int(reference.group(1)), b"")
    font_section = re.search(rb"/Font\s*<<(.*?)>>", resource, re.DOTALL)
    if font_section:
        font_resources = font_section.group(1)
    else:
        font_reference = re.search(rb"/Font\s+(\d+)\s+\d+\s+R", resource)
        if not font_reference:
            return {}
        font_resources = objects.get(int(font_reference.group(1)), b"")
    fonts: dict[bytes, tuple[dict[bytes, str] | None, str]] = {}
    for name, reference in re.findall(rb"/([^\s/<>()\[\]]+)\s+(\d+)\s+\d+\s+R", font_resources):
        font = objects.get(int(reference), b"")
        cmap_ref = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", font)
        if cmap_ref:
            cmap_object = objects.get(int(cmap_ref.group(1)), b"")
            fonts[name] = (_pdf_cmap(_pdf_stream(cmap_object)), "")
            continue
        encoding_source = font
        encoding_ref = re.search(rb"/Encoding\s+(\d+)\s+\d+\s+R", font)
        if encoding_ref:
            encoding_source = objects.get(int(encoding_ref.group(1)), b"")
        encoding = re.search(
            rb"/BaseEncoding\s*/([^\s/<>()\[\]]+)|/Encoding\s*/([^\s/<>()\[\]]+)",
            encoding_source,
        )
        encoding_name = next((part.decode("ascii") for part in encoding.groups() if part), "StandardEncoding") if encoding else "StandardEncoding"
        codecs = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman", "StandardEncoding": "cp1252"}
        if encoding_name not in codecs:
            raise ExtractionReviewError(
                f"PDF font {name.decode('ascii', 'replace')!r} uses unsupported encoding {encoding_name!r}; export a new text-based PDF"
            )
        mapping = None
        differences = re.search(rb"/Differences\s*\[(.*?)\]", encoding_source, re.DOTALL)
        if differences:
            mapping = {}
            for value in range(256):
                try:
                    mapping[bytes([value])] = bytes([value]).decode(codecs[encoding_name])
                except UnicodeDecodeError:
                    pass
            code: int | None = None
            for item in re.findall(rb"\d+|/[^\s/<>()\[\]]+", differences.group(1)):
                if item.isdigit():
                    code = int(item)
                elif code is not None and code < 256:
                    mapping[bytes([code])] = _pdf_glyph(item[1:].decode("ascii"))
                    code += 1
        fonts[name] = (mapping, codecs[encoding_name])
    return fonts


def _pdf_glyph(name: str) -> str:
    names = {"space": " ", "hyphen": "-", "period": ".", "comma": ",", "colon": ":", "semicolon": ";"}
    if name in names:
        return names[name]
    if len(name) == 1:
        return name
    match = re.fullmatch(r"(?:uni|u)([0-9A-Fa-f]{4,6})", name)
    if match:
        return chr(int(match.group(1), 16))
    raise ExtractionReviewError(f"PDF font glyph {name!r} cannot be decoded reliably; export a new text-based PDF")


def _pdf_cmap(stream: bytes) -> dict[bytes, str]:
    mapping: dict[bytes, str] = {}

    def decoded(token: bytes) -> str:
        value = bytes.fromhex(token.decode("ascii"))
        return value.decode("utf-16") if value.startswith((b"\xfe\xff", b"\xff\xfe")) else value.decode("utf-16-be")

    for section in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.DOTALL):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section):
            mapping[bytes.fromhex(source.decode("ascii"))] = decoded(target)
    for section in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.DOTALL):
        for row in section.splitlines():
            parts = re.match(rb"\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(.*)", row)
            if not parts:
                continue
            first = bytes.fromhex(parts.group(1).decode("ascii"))
            last = bytes.fromhex(parts.group(2).decode("ascii"))
            targets = re.findall(rb"<([0-9A-Fa-f]+)>", parts.group(3))
            if not targets:
                continue
            for offset, code in enumerate(range(int.from_bytes(first, "big"), int.from_bytes(last, "big") + 1)):
                target = targets[offset] if len(targets) > 1 else (int(targets[0], 16) + offset).to_bytes(len(targets[0]) // 2, "big").hex().encode()
                mapping[code.to_bytes(len(first), "big")] = decoded(target)
    if not mapping:
        raise ExtractionReviewError("PDF ToUnicode map is unreadable; export a new text-based PDF")
    return mapping


def _pdf_text(stream: bytes, fonts: dict[bytes, tuple[dict[bytes, str] | None, str]] | None = None) -> str:
    values: list[str] = []
    fonts = fonts or {}
    font: tuple[dict[bytes, str] | None, str] | None = None
    x = y = 0.0
    previous: tuple[float, float] | None = None
    for match in _PDF_EVENT.finditer(stream):
        if match.group("font"):
            if match.group("font") not in fonts:
                raise ExtractionReviewError("PDF text references an unresolved font; export a new text-based PDF")
            font = fonts[match.group("font")]
        elif match.group("tdx"):
            x += float(match.group("tdx"))
            y += float(match.group("tdy"))
        elif match.group("tmx"):
            x, y = float(match.group("tmx")), float(match.group("tmy"))
        else:
            if previous is not None and y == previous[1] and x < previous[0]:
                raise ExtractionReviewError("PDF text positioning makes reading order unreliable; export a linearized text-based PDF")
            previous = (x, y)
            if match.group("single"):
                values.append(_decode_pdf_string(match.group("single"), font))
            else:
                values.append("".join(_decode_pdf_string(item, font) for item in re.findall(_PDF_STRING, match.group("array") or b"")))
    return " ".join(value for value in values if value)


def _decode_pdf_string(token: bytes, font: tuple[dict[bytes, str] | None, str] | None = None) -> str:
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
    if font:
        mapping, codec = font
        if mapping is None:
            return value.decode(codec)
        result = []
        sizes = sorted({len(key) for key in mapping}, reverse=True)
        while value:
            key = next((value[:size] for size in sizes if value[:size] in mapping), None)
            if key is None:
                raise ExtractionReviewError("PDF font map does not cover all displayed text; export a new text-based PDF")
            result.append(mapping[key])
            value = value[len(key) :]
        return "".join(result)
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


def _date_is_grounded(profile_date: ProfileDate, text: str) -> bool:
    year = str(profile_date.value.year)
    if profile_date.precision.value == "year":
        return re.search(rf"(?<!\d){year}(?!\d)", text) is not None
    month = profile_date.value.month
    month_name = profile_date.value.strftime("%B")
    month_short = profile_date.value.strftime("%b")
    if profile_date.precision.value == "month":
        patterns = (
            rf"\b(?:{month_name}|{month_short})\.?\s+{year}\b",
            rf"(?<!\d){year}[-/.]0?{month}(?!\d)",
            rf"(?<!\d)0?{month}[-/.]{year}(?!\d)",
        )
    else:
        day = profile_date.value.day
        patterns = (
            rf"\b(?:{month_name}|{month_short})\.?\s+0?{day}(?:st|nd|rd|th)?[,]?\s+{year}\b",
            rf"(?<!\d)0?{day}\s+(?:{month_name}|{month_short})\.?\s+{year}\b",
            rf"(?<!\d){year}[-/.]0?{month}[-/.]0?{day}(?!\d)",
            rf"(?<!\d)0?{month}[-/.]0?{day}[-/.]{year}(?!\d)",
        )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _wording_is_grounded(wording: str, text: str) -> bool:
    normalized = _normalized(wording)
    return bool(normalized and re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text, re.IGNORECASE))


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
        source_text = "\n".join(extraction.resolve_source(source_id).text for source_id in interval.source_ids)
        if not _date_is_grounded(interval.start, source_text):
            raise ValueError("experience interval start date is not present in its CV sources")
        if interval.present:
            if not re.search(r"\b(?:present|current(?:ly)?)\b", source_text, re.IGNORECASE):
                raise ValueError("Present experience interval is not present in its CV sources")
        elif interval.end is not None and not _date_is_grounded(interval.end, source_text):
            raise ValueError("experience interval end date is not present in its CV sources")
        if interval.capability and not _wording_is_grounded(interval.capability, source_text):
            raise ValueError("experience interval capability is not present in its CV sources")
    for qualification in profile.qualifications:
        source_text = "\n".join(
            extraction.resolve_source(source_id).text for source_id in qualification.source_ids
        )
        for source_id in qualification.source_ids:
            extraction.resolve_quote(source_id, qualification.original_title)
        state_markers = {
            "completed": r"\b(?:completed|certified|earned|graduated|awarded|conferred|obtained)\b",
            "in_progress": r"\b(?:in[ -]progress|ongoing|currently|pursuing|studying|enrolled|candidate)\b",
        }
        marker = state_markers.get(qualification.state.value)
        negated = re.search(
            r"\b(?:not|never)\s+(?:completed|certified|earned|graduated|awarded|conferred|obtained)\b",
            source_text,
            re.IGNORECASE,
        )
        if marker and (not re.search(marker, source_text, re.IGNORECASE) or negated):
            raise ValueError(f"qualification state {qualification.state.value!r} is not present in its CV sources")
    if profile.experience_years is not None:
        years = format(profile.experience_years, "g")
        if not re.search(
            rf"(?<![\d.]){re.escape(years)}(?:\.0+)?\s*\+?\s*(?:years?|yrs?)\b",
            extraction.normalized_text,
            re.IGNORECASE,
        ):
            raise ValueError("experience_years is not explicitly supported by the CV")
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
