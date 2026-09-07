from __future__ import annotations

from datetime import date
from pathlib import Path
import json

import pymupdf  # package name: PyMuPDF

from . import __version__
from .hashing import content_sha256, file_sha256
from .provenance import SIDECAR_NAME, harvest

_AUTO_FALLBACK_THRESHOLD = 150  # total chars across all pages


def _extract_pages_markdown(pdf_path: Path) -> list[dict]:
    """Per-page markdown, with tables rendered in place.

    `pymupdf4llm` keeps table values in their own columns, and where it cannot
    separate two columns it merges them under a joint header
    (`**MIN**<br>**TYP**`) instead of guessing. That is the whole reason for
    preferring it over assembling tables from `find_tables()`, which collapsed
    numeric columns into one cell and then repeated that cell across every
    column it spanned - asserting one value as MIN, TYP and MAX alike.

    Costs roughly 0.2s per page against 0.02s for plain text, and yields
    4-26% more characters. Step 1 is cached, so that is paid once per
    datasheet however many times it is profiled.
    """
    import pymupdf4llm
    from pymupdf4llm.ocr import OCRMode

    # OCR is disabled deliberately. Left to itself pymupdf4llm runs OCR over
    # pages with no text layer whenever any OCR backend happens to be
    # importable - and installing `docling` for the separate OCR extractor
    # makes one importable. That would make step-1 output, and therefore
    # content_sha256, depend on which unrelated optional packages a machine
    # has: the same PDF would hash differently for two people. Measured on
    # ADS114S06B it changed the output by 2,461 characters and added 9
    # seconds. Scanned PDFs are served by the explicit `docling` extractor.
    chunks = pymupdf4llm.to_markdown(
        str(pdf_path), page_chunks=True, show_progress=False, use_ocr=OCRMode.NEVER
    )
    pages: list[dict] = []
    for index, chunk in enumerate(chunks):
        # page_number is 1-based; fall back to position if it is ever absent.
        number = (chunk.get("metadata") or {}).get("page_number") or index + 1
        pages.append({"page_num": number, "text": (chunk.get("text") or "").strip()})
    return pages


def _extract_pages_pymupdf(pdf_path: Path) -> list[dict]:
    """Plain per-page text, with no table structure.

    Kept as the dependency-light path: it needs only PyMuPDF, where the
    markdown extractor pulls in pymupdf4llm, pymupdf-layout and onnxruntime.
    """
    doc = pymupdf.open(pdf_path)
    return [
        {"page_num": index + 1, "text": page.get_text("text").strip()}
        for index, page in enumerate(doc)
    ]


def _total_chars(pages: list[dict]) -> int:
    return sum(len(p["text"]) for p in pages)


def extract_pdf_text(pdf_path: Path, extractor: str = "pymupdf4llm", *,
                     source_url: str | None = None,
                     sidecar: dict[str, dict] | None = None) -> dict:
    """
    Extract PDF text using the specified backend.

    extractor:
      pymupdf4llm - per-page markdown with tables rendered in place (default)
      pymupdf     - plain text only; no table structure, but needs no extra
                    dependencies beyond PyMuPDF
      docling     - OCR for image-heavy or scanned PDFs
      auto        - try pymupdf4llm first; fall back to docling if the PDF has
                    almost no text layer (< 150 chars)

    Provenance (where the PDF came from) is captured here because this is the
    only step that touches the original file, and the Zone.Identifier stream
    it reads does not survive being copied off NTFS.
    """
    source_hash = file_sha256(pdf_path)
    used = extractor

    if extractor == "pymupdf4llm":
        pages = _extract_pages_markdown(pdf_path)

    elif extractor == "pymupdf":
        pages = _extract_pages_pymupdf(pdf_path)

    elif extractor == "docling":
        from .docling_extract import extract_pdf_text_docling
        pages = extract_pdf_text_docling(pdf_path)

    elif extractor == "auto":
        pages = _extract_pages_markdown(pdf_path)
        used = "pymupdf4llm"
        chars = _total_chars(pages)
        if chars < _AUTO_FALLBACK_THRESHOLD:
            print(f"  [auto] text layer yielded {chars} chars - falling back to Docling")
            from .docling_extract import extract_pdf_text_docling
            pages = extract_pdf_text_docling(pdf_path)
            used = "docling"

    else:
        raise ValueError(
            f"Unknown extractor: {extractor!r}. "
            "Choose pymupdf4llm, pymupdf, docling, or auto."
        )

    # The hashed payload excludes anything unstable (dates, tool versions) so
    # the content hash only changes when the extracted text itself changes.
    payload = {
        "file": pdf_path.name,
        "source_sha256": source_hash,
        "page_count": len(pages),
        "pages": pages,
    }
    # Provenance sits outside the hashed payload, alongside the other
    # acquisition metadata: it says where the bytes came from, not what they
    # are, so recording it must not change an existing content hash.
    prov = harvest(pdf_path, source_hash, source_url=source_url, sidecar=sidecar)
    if not prov.resolved:
        print(f"  [provenance] no source URL for {pdf_path.name} - "
              f"pass --source-url or add it to {SIDECAR_NAME}")
    elif prov.url_note:
        print(f"  [provenance] {prov.source_url}  ({prov.url_note})")

    return {
        **payload,
        "content_sha256": content_sha256(payload),
        "extractor": used,
        "extractor_version": __version__,
        "extracted_date": date.today().isoformat(),
        **prov.as_meta_fields(),
    }


def save_extracted_text(data: dict, out_path: Path) -> None:
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
