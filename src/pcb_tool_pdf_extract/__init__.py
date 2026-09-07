"""pcb_tool_pdf_extract: structured component profiles from datasheet PDFs."""

#: 0.2.0 added tables assembled from find_tables(); 0.3.0 replaced that
#: with pymupdf4llm markdown, which keeps table values in their own
#: columns. Both changed the extracted content, and so every step-1
#: content_sha256 - re-extraction is required, not optional.
__version__ = "0.3.0"
