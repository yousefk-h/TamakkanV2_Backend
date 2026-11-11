"""Plain-text extraction for every teacher-uploaded lesson document: PDF (via
the existing `app.core.pdf_extraction`), Word (.docx) and PowerPoint (.pptx).

DOCX and PPTX are both OOXML zip packages, so they are read with the standard
library (`zipfile` + `xml.etree`) rather than adding `python-docx`/
`python-pptx` - two more dependencies for what is, for lesson grounding, a
"collect every text run in document order" job. Formatting, images and
embedded media are deliberately ignored: only readable text can ground a
lesson, and anything else would have to be invented.

Mirrors `pdf_extraction`'s contract exactly: a document that cannot be read,
or yields no text at all, raises `DocumentExtractionError` so the upload
route turns it into a 422 instead of silently storing an empty source.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from app.core.pdf_extraction import PdfExtractionError, extract_pdf_text
from app.schema.limits import MAX_TRANSCRIPT_LENGTH

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF_CONTENT_TYPE = "application/pdf"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
# OOXML files are ZIP containers.  Never let a tiny compressed upload expand
# into an unbounded amount of memory while extracting lesson text.
MAX_PACKAGE_MEMBERS = 2_000
MAX_PACKAGE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_COMPRESSION_RATIO = 100


class DocumentExtractionError(Exception):
    """The file is not a readable document of the claimed kind, or has no text."""


def detect_document_kind(content_type: str, filename: str) -> str:
    """`pdf`, `docx` or `pptx` - the browser's content type first, the file
    extension as a fallback (some browsers send `application/octet-stream`
    for Office files). Anything else is rejected before any bytes are read."""
    lowered = (filename or "").lower()
    if content_type == PDF_CONTENT_TYPE or lowered.endswith(".pdf"):
        return "pdf"
    if content_type == DOCX_CONTENT_TYPE or lowered.endswith(".docx"):
        return "docx"
    if content_type == PPTX_CONTENT_TYPE or lowered.endswith(".pptx"):
        return "pptx"
    raise DocumentExtractionError("Upload a PDF, Word (.docx) or PowerPoint (.pptx) document")


def _open_package(file_bytes: bytes, label: str) -> zipfile.ZipFile:
    try:
        package = zipfile.ZipFile(BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise DocumentExtractionError(f"Could not read this file as a {label} document") from exc
    infos = package.infolist()
    total_size = sum(info.file_size for info in infos)
    if len(infos) > MAX_PACKAGE_MEMBERS or total_size > MAX_PACKAGE_UNCOMPRESSED_BYTES:
        package.close()
        raise DocumentExtractionError(f"This {label} document is too complex to read safely")
    for info in infos:
        if info.flag_bits & 0x1 or (
            info.compress_size and info.file_size / info.compress_size > MAX_PACKAGE_COMPRESSION_RATIO
        ):
            package.close()
            raise DocumentExtractionError(f"This {label} document is not safe to read")
    return package


def _paragraph_texts(root: ElementTree.Element, paragraph_tag: str, run_tag: str) -> list[str]:
    paragraphs: list[str] = []
    for paragraph in root.iter(paragraph_tag):
        text = "".join(node.text or "" for node in paragraph.iter(run_tag)).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def extract_docx_text(file_bytes: bytes) -> str:
    with _open_package(file_bytes, "Word") as package:
        try:
            xml = package.read("word/document.xml")
        except KeyError as exc:
            raise DocumentExtractionError("This file is not a Word (.docx) document") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DocumentExtractionError("Could not read this Word document") from exc
    paragraphs = _paragraph_texts(root, f"{_W}p", f"{_W}t")
    return _finish("\n".join(paragraphs), "Word document")


def extract_pptx_text(file_bytes: bytes) -> str:
    with _open_package(file_bytes, "PowerPoint") as package:
        slides = sorted(
            ((int(match.group(1)), name) for name in package.namelist() if (match := _SLIDE_RE.match(name))),
        )
        if not slides:
            raise DocumentExtractionError("This file is not a PowerPoint (.pptx) presentation")
        chunks: list[str] = []
        for number, name in slides:
            try:
                root = ElementTree.fromstring(package.read(name))
            except ElementTree.ParseError as exc:
                raise DocumentExtractionError(f"Could not read slide {number}") from exc
            paragraphs = _paragraph_texts(root, f"{_A}p", f"{_A}t")
            if paragraphs:
                chunks.append(f"Slide {number}\n" + "\n".join(paragraphs))
    return _finish("\n\n".join(chunks), "presentation")


def _finish(text: str, label: str) -> str:
    text = text.strip()
    if not text:
        raise DocumentExtractionError(f"No readable text found in this {label} - it isn't usable as lesson source material")
    return text[:MAX_TRANSCRIPT_LENGTH]


def extract_document_text(file_bytes: bytes, content_type: str, filename: str) -> tuple[str, str]:
    """Returns `(kind, text)` for an uploaded lesson document of any supported
    kind, raising `DocumentExtractionError` for anything unreadable."""
    kind = detect_document_kind(content_type, filename)
    if kind == "pdf":
        try:
            return kind, extract_pdf_text(file_bytes)
        except PdfExtractionError as exc:
            raise DocumentExtractionError(str(exc)) from exc
    if kind == "docx":
        return kind, extract_docx_text(file_bytes)
    return kind, extract_pptx_text(file_bytes)
