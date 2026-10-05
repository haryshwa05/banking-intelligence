"""Text extraction for Word and plain-text documents, and file-kind detection.

Word and text files have no fixed pages, so their text is split into labelled
sections of bounded size. Each section is written with the same
``--- Page N ---`` marker PDFs use, so chunking, fact extraction and citations
work unchanged; the human-readable label ("Section 2") is stored alongside.
"""

from __future__ import annotations

from pathlib import Path

PDF = "pdf"
IMAGE = "image"
SPREADSHEET = "spreadsheet"
WORD = "word"
TEXT = "text"

SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xlsm"}
WORD_EXTENSIONS = {".docx"}
TEXT_EXTENSIONS = {".txt", ".md"}
SECTION_CHARS = 3000


def document_kind(file_name: str, mime_type: str) -> str | None:
    """Classify an upload by what we can extract from it, or None if unsupported.

    The extension decides for office formats because browsers report CSV and
    Excel files with inconsistent MIME types (Windows often sends CSV as
    ``application/vnd.ms-excel``).
    """
    extension = Path(file_name).suffix.lower()
    if mime_type == "application/pdf" or extension == ".pdf":
        return PDF
    if extension in SPREADSHEET_EXTENSIONS:
        return SPREADSHEET
    if extension in WORD_EXTENSIONS:
        return WORD
    if extension in TEXT_EXTENSIONS:
        return TEXT
    if mime_type.startswith("image/"):
        return IMAGE
    return None


def decode_text(raw: bytes) -> str:
    """Decode text whose encoding is not guaranteed to be UTF-8.

    Handles Excel's "Unicode Text" export (UTF-16 with or without a BOM): read
    as UTF-8 it decodes without error but leaves NUL characters in every value.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    sample = raw[:4000]
    if sample and sample.count(b"\x00") > len(sample) * 0.3:
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def sections_to_text(blocks: list[str], label: str = "Section") -> tuple[str, int, list[str]]:
    """Group text blocks into bounded sections with page markers and labels."""
    sections: list[str] = []
    current = ""
    for block in (item.strip() for item in blocks):
        if not block:
            continue
        if current and len(current) + len(block) + 2 > SECTION_CHARS:
            sections.append(current)
            current = ""
        # A single oversized block is split rather than truncated.
        while len(block) > SECTION_CHARS:
            cut = block.rfind("\n", 0, SECTION_CHARS)
            cut = cut if cut > SECTION_CHARS // 2 else SECTION_CHARS
            if current:
                sections.append(current)
                current = ""
            sections.append(block[:cut].strip())
            block = block[cut:].strip()
        current = f"{current}\n\n{block}" if current else block
    if current:
        sections.append(current)
    if not sections:
        return "", 0, []
    text = "\n\n".join(f"--- Page {index} ---\n{body}" for index, body in enumerate(sections, start=1))
    labels = [f"{label} {index}" for index in range(1, len(sections) + 1)]
    return text, len(sections), labels


def extract_word(path: Path) -> tuple[str, int, list[str]]:
    """Paragraphs and tables of a .docx file, in document order."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    blocks: list[str] = []
    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            if style.startswith("Heading"):
                level = next((int(char) for char in style if char.isdigit()), 1)
                text = f"{'#' * level} {text}"
            blocks.append(text)
        elif tag == "tbl":
            rows: list[str] = []
            for row in Table(element, document).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = " ".join(cell.text.split())
                    # Merged cells repeat; keep one copy so values are not duplicated.
                    if not cells or cells[-1] != value:
                        cells.append(value)
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append("\n".join(rows))
    return sections_to_text(blocks)


def extract_plain_text(path: Path) -> tuple[str, int, list[str]]:
    text = decode_text(path.read_bytes()).replace("\r\n", "\n").replace("\x00", "")
    return sections_to_text(text.split("\n\n"))
