from __future__ import annotations

from pathlib import Path

_DOCLING_AVAILABLE = False
_DOCLING_IMPORT_ERROR: str | None = None

try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    try:
        # docling >= 2.85 dropped the "2" from the class name.
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend as _PdfiumBackend
    except ImportError:
        from docling.backend.pypdfium2_backend import (  # type: ignore[no-redef]
            PyPdfium2DocumentBackend as _PdfiumBackend,
        )
    _DOCLING_AVAILABLE = True
except (ImportError, OSError) as _e:
    _DOCLING_IMPORT_ERROR = str(_e)


def extract_pdf_text_docling(pdf_path: Path) -> list[dict]:
    """
    Extract text per page using Docling with OCR and table structure enabled.
    Tables are rendered as markdown so they survive chunking and are useful
    to the LLM. Returns [{"page_num": int, "text": str}, ...].
    """
    if not _DOCLING_AVAILABLE:
        raise RuntimeError(
            f"Docling is not available: {_DOCLING_IMPORT_ERROR}\n"
            "On Windows, Smart App Control may block pypdfium2's native DLL. "
            "Run: pip install docling  or check Windows Security settings."
        )

    print("  [docling] Converting PDF (first run downloads OCR models ~300 MB)...")

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                backend=_PdfiumBackend,
            )
        }
    )

    result = converter.convert(str(pdf_path))
    doc = result.document

    # Group content items by page using provenance info.
    # Tables are exported to markdown; all other items use their text.
    pages_content: dict[int, list[str]] = {}

    for item, _level in doc.iterate_items():
        if not getattr(item, "prov", None):
            continue
        page_no = item.prov[0].page_no

        # TableItem has export_to_markdown(); text items have .text.
        # Newer docling requires the document to be passed in, and some item
        # types (PictureItem) only accept that form - so try both, and skip
        # the item rather than failing the whole extraction over one figure.
        if hasattr(item, "export_to_markdown"):
            try:
                fragment = item.export_to_markdown(doc)
            except TypeError:
                fragment = item.export_to_markdown()
            except Exception:
                continue
        elif hasattr(item, "text") and item.text:
            fragment = item.text
        else:
            continue

        if fragment and fragment.strip():
            pages_content.setdefault(page_no, []).append(fragment.strip())

    # doc.pages is a dict {page_no: PageItem} in Docling 2.x
    page_count = len(doc.pages) if getattr(doc, "pages", None) else (
        max(pages_content.keys(), default=1)
    )

    pages = [
        {
            "page_num": page_no,
            "text": "\n\n".join(pages_content.get(page_no, [])),
        }
        for page_no in range(1, page_count + 1)
    ]

    # Fallback: if per-page provenance produced nothing, use full-doc markdown
    if not any(p["text"] for p in pages):
        full_md = doc.export_to_markdown()
        pages = [{"page_num": 1, "text": full_md}]

    return pages
