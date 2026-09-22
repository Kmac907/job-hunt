from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from job_hunt.cv import ExtractionReviewError, extract_cv, supported_duration
from job_hunt.models import ProfileDate, SupportedInterval


def test_text_line_ranges_are_stable_and_quotes_resolve(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Experience\nBuilt APIs\n\nSkills\nPython\n", encoding="utf-8")

    first = extract_cv(path, required_sections=("Experience", "Skills"))
    second = extract_cv(path, required_sections=("Experience", "Skills"))

    assert first == second
    assert [block.source_id for block in first.blocks] == ["text:lines:1-2", "text:lines:4-5"]
    assert first.resolve_quote("text:lines:1-2", "Built APIs").location == "lines 1-2"


def test_docx_paragraphs_and_table_cells_have_stable_sources(tmp_path: Path) -> None:
    path = tmp_path / "cv.docx"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:p><w:r><w:t>Experience</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Python</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)

    snapshot = extract_cv(path)

    assert [item.source_id for item in snapshot.blocks] == [
        "docx:paragraph:1",
        "docx:table:1:row:1:cell:1",
    ]
    assert snapshot.normalized_text == "Experience\nPython"


def test_docx_preserves_tabs_breaks_and_cell_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "formatted.docx"
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:p><w:r><w:t>Senior</w:t><w:tab/><w:t>Engineer</w:t><w:br/><w:t>Remote</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Python</w:t></w:r></w:p><w:p><w:r><w:t>Go</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)

    snapshot = extract_cv(path)

    assert [block.text for block in snapshot.blocks] == ["Senior Engineer\nRemote", "Python\nGo"]


def test_docx_content_controls_are_extracted_in_document_order(tmp_path: Path) -> None:
    path = tmp_path / "controlled.docx"
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:sdt><w:sdtContent><w:p><w:r><w:t>Skills: Python</w:t></w:r></w:p></w:sdtContent></w:sdt>
      <w:p><w:r><w:t>Experience</w:t></w:r></w:p>
    </w:body></w:document>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)

    snapshot = extract_cv(path, required_sections=("Skills", "Experience"))

    assert [block.text for block in snapshot.blocks] == ["Skills: Python", "Experience"]


def test_text_pdf_pages_are_extracted_and_image_only_files_stop(tmp_path: Path) -> None:
    text_pdf = tmp_path / "cv.pdf"
    text_pdf.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Page /Contents 2 0 R >> endobj\n"
        b"2 0 obj << /Length 34 >> stream\nBT (Python engineer) Tj ET\nendstream\nendobj\n%%EOF"
    )
    snapshot = extract_cv(text_pdf)
    assert snapshot.blocks[0].source_id == "pdf:page:1"
    assert snapshot.resolve_quote("pdf:page:1", "Python engineer")

    image_pdf = tmp_path / "scan.pdf"
    image_pdf.write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Page /Subtype /Image >> endobj\n%%EOF")
    with pytest.raises(ExtractionReviewError, match="image-only"):
        extract_cv(image_pdf)


def test_pdf_uses_font_cmaps_and_rejects_unreliable_position_order(tmp_path: Path) -> None:
    mapped = tmp_path / "mapped.pdf"
    mapped.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Page /Resources << /Font << /F1 3 0 R >> >> /Contents 2 0 R >> endobj\n"
        b"2 0 obj << >> stream\nBT /F1 12 Tf <41> Tj ET\nendstream\nendobj\n"
        b"3 0 obj << /Type /Font /ToUnicode 4 0 R >> endobj\n"
        b"4 0 obj << >> stream\n1 beginbfchar\n<41> <0050007900740068006F006E>\nendbfchar\nendstream\nendobj\n%%EOF"
    )
    assert extract_cv(mapped).blocks[0].text == "Python"

    encoded = tmp_path / "encoded.pdf"
    encoded.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Page /Resources << /Font << /F1 3 0 R >> >> /Contents 2 0 R >> endobj\n"
        b"2 0 obj << >> stream\nBT /F1 12 Tf <414243444546> Tj ET\nendstream\nendobj\n"
        b"3 0 obj << /Type /Font /Encoding << /BaseEncoding /WinAnsiEncoding /Differences [65 /P /y /t /h /o /n] >> >> endobj\n%%EOF"
    )
    assert extract_cv(encoded).blocks[0].text == "Python"

    reordered = tmp_path / "reordered.pdf"
    reordered.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Page /Contents 2 0 R >> endobj\n"
        b"2 0 obj << >> stream\nBT 300 700 Td (Right column) Tj -300 0 Td (Left column) Tj ET\nendstream\nendobj\n%%EOF"
    )
    with pytest.raises(ExtractionReviewError, match="reading order unreliable"):
        extract_cv(reordered)


def test_pdf_page_tree_defines_page_ids_and_ambiguous_order_stops(tmp_path: Path) -> None:
    ordered = tmp_path / "ordered.pdf"
    ordered.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 5 0 R >> endobj\n"
        b"2 0 obj << /Type /Page /Contents 3 0 R >> endobj\n"
        b"3 0 obj << >> stream\nBT (Second) Tj ET\nendstream\nendobj\n"
        b"4 0 obj << /Type /Page /Contents 6 0 R >> endobj\n"
        b"5 0 obj << /Type /Pages /Kids [4 0 R 2 0 R] >> endobj\n"
        b"6 0 obj << >> stream\nBT (First) Tj ET\nendstream\nendobj\n%%EOF"
    )
    snapshot = extract_cv(ordered)
    assert [(block.source_id, block.text) for block in snapshot.blocks] == [
        ("pdf:page:1", "First"),
        ("pdf:page:2", "Second"),
    ]

    ambiguous = tmp_path / "ambiguous.pdf"
    ambiguous.write_bytes(
        b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n"
        b"2 0 obj << /Type /Page >> endobj\n%%EOF"
    )
    with pytest.raises(ExtractionReviewError, match="page order is unreliable"):
        extract_cv(ambiguous)


def test_bad_or_incomplete_inputs_require_actionable_review(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 truncated")
    with pytest.raises(ExtractionReviewError, match="malformed or truncated"):
        extract_cv(broken)

    incomplete = tmp_path / "cv.txt"
    incomplete.write_text("Experience\nDeveloper", encoding="utf-8")
    with pytest.raises(ExtractionReviewError, match="required section 'Skills'"):
        extract_cv(incomplete, required_sections=("Skills",))

    incomplete.write_text("Experience\nDiscussed softskills", encoding="utf-8")
    with pytest.raises(ExtractionReviewError, match="required section 'Skills'"):
        extract_cv(incomplete, required_sections=("Skills",))


def test_supported_duration_merges_overlap_and_separates_capability() -> None:
    intervals = [
        SupportedInterval(
            start=ProfileDate(value=date(2024, 1, 1), precision="day"),
            end=ProfileDate(value=date(2024, 1, 31), precision="day"),
            source_ids=["one"],
            capability="Python",
        ),
        SupportedInterval(
            start=ProfileDate(value=date(2024, 1, 15), precision="day"),
            end=ProfileDate(value=date(2024, 2, 15), precision="day"),
            source_ids=["two"],
            capability="Go",
        ),
        SupportedInterval(
            start=ProfileDate(value=date(2024, 2, 1), precision="day"),
            present=True,
            source_ids=["three"],
            capability="Go",
        ),
    ]

    overall = supported_duration(intervals, as_of=date(2024, 2, 29))
    python = supported_duration(intervals, as_of=date(2024, 2, 29), capability="Python")

    assert (overall.minimum_days, overall.maximum_days) == (60, 60)
    assert (python.minimum_days, python.maximum_days) == (31, 31)


def test_date_precision_is_retained_in_duration_bounds() -> None:
    interval = SupportedInterval(
        start=ProfileDate(value=date(2020, 1, 1), precision="month"),
        end=ProfileDate(value=date(2020, 3, 1), precision="month"),
        source_ids=["month-range"],
    )
    result = supported_duration([interval], as_of=date(2024, 1, 1))
    assert result.minimum_days < result.maximum_days
    assert interval.start.precision.value == "month"


def test_duration_bounds_allow_mixed_precision_and_use_as_of_for_present() -> None:
    uncertain = SupportedInterval(
        start=ProfileDate(value=date(2024, 3, 1), precision="month"),
        end=ProfileDate(value=date(2024, 1, 1), precision="year"),
        source_ids=["mixed"],
    )
    same_year = SupportedInterval(
        start=ProfileDate(value=date(2020, 1, 1), precision="year"),
        end=ProfileDate(value=date(2020, 1, 1), precision="year"),
        source_ids=["year"],
    )
    present = SupportedInterval(
        start=ProfileDate(value=date(2024, 1, 1), precision="month"),
        present=True,
        source_ids=["present"],
    )

    assert supported_duration([uncertain], as_of=date(2025, 1, 1)).minimum_days == 1
    assert supported_duration([same_year], as_of=date(2025, 1, 1)).minimum_days == 1
    assert supported_duration([present], as_of=date(2024, 2, 10)).maximum_days == 41

    future = present.model_copy(
        update={"start": ProfileDate(value=date(2025, 1, 1), precision="day")}
    )
    with pytest.raises(ValueError, match="as-of"):
        supported_duration([future], as_of=date(2024, 2, 10))

    with pytest.raises(ValidationError, match="cannot precede"):
        SupportedInterval(
            start=ProfileDate(value=date(2025, 1, 1), precision="year"),
            end=ProfileDate(value=date(2024, 1, 1), precision="year"),
            source_ids=["bad"],
        )
