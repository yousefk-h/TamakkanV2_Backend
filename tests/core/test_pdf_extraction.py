"""`app.core.pdf_extraction` - video-optional lesson source (card #16)."""

import pytest

from app.core.pdf_extraction import PdfExtractionError, extract_pdf_text


def build_minimal_pdf(text: bytes = b"Hello PDF World") -> bytes:
    """A hand-built, single-page PDF with one line of real text - just
    enough structure (a correct xref table + `startxref`) for `pypdf` to
    parse it, so tests don't need a real document fixture or an extra
    PDF-authoring dependency.
    """
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = b"BT /F1 24 Tf 10 100 Td (" + text + b") Tj ET"
    objects.append(b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + b"\n" + obj + b"\nendobj\n"
    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<</Size " + str(count).encode() + b"/Root 1 0 R>>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"
    return bytes(out)


def test_extract_pdf_text_returns_the_pdfs_text():
    assert extract_pdf_text(build_minimal_pdf(b"Hello PDF World")) == "Hello PDF World"


def test_extract_pdf_text_rejects_garbage_bytes():
    with pytest.raises(PdfExtractionError):
        extract_pdf_text(b"this is not a pdf at all")


def test_extract_pdf_text_rejects_a_pdf_with_no_text_layer():
    """A scanned/image-only PDF (no text objects at all) must fail clearly,
    not come back as an empty string the caller would silently store.
    """
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>",
        b"<</Length 0>>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + b"\n" + obj + b"\nendobj\n"
    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<</Size " + str(count).encode() + b"/Root 1 0 R>>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"

    with pytest.raises(PdfExtractionError):
        extract_pdf_text(bytes(out))
