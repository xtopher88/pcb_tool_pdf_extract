from __future__ import annotations

from datetime import date
from pathlib import Path
import json

import pymupdf  # package name: PyMuPDF

from . import __version__
from .hashing import content_sha256, file_sha256
from .provenance import SIDECAR_NAME, harvest

_AUTO_FALLBACK_THRESHOLD = 150  # total chars across all pages


def _extract_pages_pymupdf(pdf_path: Path) -> list[dict]:
    doc = pymupdf.open(pdf_path)
    return [
        {"page_num": i + 1, "text": page.get_text("text").strip()}
        for i, page in enumerate(doc)
    ]


def _total_chars(pages: list[dict]) -> int:
    return sum(len(p["text"]) for p in pages)


def extract_pdf_text(pdf_path: Path, extractor: str = "pymupdf", *,
                     source_url: str | None = None,
                     sidecar: dict[str, dict] | None = None) -> dict:
    """
    Extract PDF text using the specified backend.

    extractor:
      pymupdf  - fast text-layer extraction (default, works for most IC datasheets)
      docling  - OCR + table extraction for image-heavy or scanned PDFs
      auto     - try pymupdf first; fall back to docling if < 150 chars extracted

    Provenance (where the PDF came from) is captured here because this is the
    only step that touches the original file, and the Zone.Identifier stream
    it reads does not survive being copied off NTFS.
    """
    source_hash = file_sha256(pdf_path)
    used = extractor

    if extractor == "pymupdf":
        pages = _extract_pages_pymupdf(pdf_path)

    elif extractor == "docling":
        from .docling_extract import extract_pdf_text_docling
        pages = extract_pdf_text_docling(pdf_path)

    elif extractor == "auto":
        pages = _extract_pages_pymupdf(pdf_path)
        used = "pymupdf"
        chars = _total_chars(pages)
        if chars < _AUTO_FALLBACK_THRESHOLD:
            print(f"  [auto] PyMuPDF yielded {chars} chars - falling back to Docling")
            from .docling_extract import extract_pdf_text_docling
            pages = extract_pdf_text_docling(pdf_path)
            used = "docling"

    else:
        raise ValueError(
            f"Unknown extractor: {extractor!r}. Choose pymupdf, docling, or auto."
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
