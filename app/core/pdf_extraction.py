"""Plain-text extraction from a teacher-uploaded lesson PDF (card #16: video
must not be required). Pure-Python (`pypdf`, no external binary/OCR step) -
good enough for the typeset, text-layer PDFs a lesson document actually is;
a scanned image-only PDF yields no text and is rejected at upload time (see
`extract_pdf_text`'s docstring) rather than silently stored empty.

The extracted text is what `TranscriptPipelineService` treats as this
topic's non-video source material - wrapped as a single `TranscriptSegment`
exactly like a transcribed video's, so the existing review/approve/draft
pipeline needs no new states to support it (see that module's docstring).
"""

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.schema.limits import MAX_TRANSCRIPT_LENGTH


class PdfExtractionError(Exception):
    """Raised when a PDF can't be read at all, or yields no usable text -
    e.g. a scanned/image-only PDF with no text layer. The caller turns this
    into a 422, not a 500: the upload itself succeeded, the file just isn't
    usable as grounding material.
    """


def extract_pdf_text(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise PdfExtractionError(f"Could not read this file as a PDF: {exc}") from exc
    except Exception as exc:
        raise PdfExtractionError(f"Could not read this file as a PDF: {exc}") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
    if not text:
        raise PdfExtractionError(
            "No readable text found in this PDF - a scanned/image-only document "
            "isn't usable as lesson source material yet."
        )
    return text[:MAX_TRANSCRIPT_LENGTH]
