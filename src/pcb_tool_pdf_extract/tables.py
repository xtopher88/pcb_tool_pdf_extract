"""Checking the table markdown that step 1 produces.

Extraction no longer builds tables itself. `pymupdf4llm` renders each page as
markdown with its tables integrated, which is better than anything assembled
from `find_tables()` here: it keeps values in their own columns, and where it
cannot separate two columns it merges them under a joint header
(`**MIN**<br>**TYP**`) rather than guessing.

What remains is a narrow watch for the one failure that would be silent. The
discarded `find_tables()` path repeated a spanned cell into every column it
covered, so a value belonging to one column was asserted for three:

    |±0.5|±0.5|±0.5|nA|        # MIN, TYP and MAX all claimed to be ±0.5

Nothing else about that row looks wrong, and the resulting profile would carry
a fabricated minimum and maximum with no other symptom.

The check is deliberately narrow. A first attempt flagged any value repeated
across three adjacent columns and produced 28 hits on one datasheet, every one
legitimate - a resolution of "16 (16)" really is the same at every gain, and
block-protection tables really are mostly zeros. A watchdog that cries wolf is
worse than none, so only columns whose header promises one value per row
(MIN/TYP/MAX and friends) are compared.
"""
from __future__ import annotations

import re

#: A markdown table row: leading pipe, cells, trailing pipe.
_ROW = re.compile(r"^\s*\|(.+)\|\s*$")

#: Separator rows (|---|---|) are structural, not data.
_SEPARATOR_CELL = re.compile(r"^[\s:-]*$")

#: Markdown decoration inside a header cell.
_DECORATION = re.compile(r"<[^>]{1,20}>|[*_`]+")

#: Header naming a column that holds a single value per row. A value repeated
#: across all of these is the fabrication this module watches for.
_SPEC_HEADER = re.compile(r"^(min|max|typ|typical|nom|nominal)\b", re.IGNORECASE)

#: Fewer spec columns than this cannot show the pattern convincingly.
_MIN_SPEC_COLUMNS = 3


def _cells(line: str) -> list[str] | None:
    match = _ROW.match(line)
    if not match:
        return None
    return [cell.strip() for cell in match.group(1).split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(_SEPARATOR_CELL.match(cell) for cell in cells)


def _spec_columns(header: list[str]) -> list[int]:
    """Indices of header cells promising a single value per row."""
    return [
        index
        for index, cell in enumerate(header)
        if _SPEC_HEADER.match(_DECORATION.sub("", cell).strip())
    ]


def replicated_rows(markdown: str) -> list[str]:
    """Rows asserting one value across every MIN/TYP/MAX column of a table.

    A table's header is the row immediately preceding its separator row, so
    the spec columns are known before its data rows are examined.
    """
    lines = markdown.splitlines()
    found: list[str] = []
    spec: list[int] = []

    for index, line in enumerate(lines):
        cells = _cells(line)
        if cells is None:
            spec = []
            continue
        if _is_separator(cells):
            continue

        # A row followed by a separator is a header: it opens a new table.
        following = _cells(lines[index + 1]) if index + 1 < len(lines) else None
        if following is not None and _is_separator(following):
            spec = _spec_columns(cells)
            continue

        if len(spec) < _MIN_SPEC_COLUMNS:
            continue
        values = [cells[i].strip() for i in spec if i < len(cells)]
        if len(values) < _MIN_SPEC_COLUMNS:
            continue
        first = values[0]
        if first and not _SEPARATOR_CELL.match(first) and all(v == first for v in values):
            found.append(line.strip())
    return found


def table_row_count(markdown: str) -> int:
    """Data rows across every markdown table on a page."""
    return sum(
        1
        for line in markdown.splitlines()
        if (cells := _cells(line)) is not None and not _is_separator(cells)
    )


def audit(pages: list[dict]) -> dict:
    """Summarise table content and any suspect rows across extracted pages."""
    rows = 0
    suspect: list[dict] = []
    for page in pages:
        text = page.get("text", "")
        rows += table_row_count(text)
        for line in replicated_rows(text):
            suspect.append({"page": page.get("page_num"), "row": line[:160]})
    return {"table_rows": rows, "suspect_rows": suspect}
