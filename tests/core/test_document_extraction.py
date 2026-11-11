import io
import zipfile

import pytest

from app.core.document_extraction import DocumentExtractionError, extract_document_text


def _package(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return output.getvalue()


def build_docx(text: str) -> bytes:
    return _package({
        "word/document.xml": f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>',
    })


def build_pptx(text: str) -> bytes:
    return _package({
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>',
    })


def test_extracts_docx_and_pptx_text():
    assert extract_document_text(build_docx("Meeting vocabulary"), "application/octet-stream", "notes.docx") == ("docx", "Meeting vocabulary")
    assert extract_document_text(build_pptx("Slide lesson"), "application/octet-stream", "slides.pptx") == ("pptx", "Slide 1\nSlide lesson")


@pytest.mark.parametrize("filename", ["notes.docx", "slides.pptx"])
def test_rejects_malformed_office_documents(filename: str):
    with pytest.raises(DocumentExtractionError):
        extract_document_text(b"not a zip package", "application/octet-stream", filename)


def test_rejects_compressed_zip_bomb():
    payload = _package({"word/document.xml": "x" * (6 * 1024 * 1024)})
    with pytest.raises(DocumentExtractionError):
        extract_document_text(payload, "application/octet-stream", "oversized.docx")
