"""Tests for the markdown table audit.

Step 1 no longer builds tables itself - `pymupdf4llm` renders each page as
markdown with its tables in place. What is left here is a watch for the one
failure that would be silent: a row asserting a single value across several
columns, which is what raw `find_tables()` used to produce and what would put
wrong numbers into a profile with no other symptom.
"""
from __future__ import annotations

from pcb_tool_pdf_extract.tables import (
    audit,
    replicated_rows,
    table_row_count,
)

# What the discarded find_tables() path produced: ±0.5 claimed as MIN, TYP
# and MAX alike.
FABRICATED = """
|**PARAMETER**|**MIN**|**TYP**|**MAX**|**UNIT**|
|---|---|---|---|---|
|Absolute input current|±0.5|±0.5|±0.5|nA|
"""

# What pymupdf4llm produces: columns it cannot separate are merged under a
# joint header rather than guessed at, so nothing is asserted falsely.
HONEST = """
||**PARAMETER**|**TEST CONDITIONS**|**MIN**<br>**TYP**|**MAX**|**UNIT**|
|---|---|---|---|---|---|
||Absolute input current|PGA bypassed|±0.5||nA|
||Differential input current|PGA enabled|–10<br>±0.1|10||
"""


def test_fabricated_row_is_flagged():
    rows = replicated_rows(FABRICATED)
    assert len(rows) == 1
    assert "±0.5" in rows[0]


def test_honest_table_is_not_flagged():
    assert replicated_rows(HONEST) == []


def test_separator_rows_are_not_mistaken_for_repetition():
    """`|---|---|---|` repeats by construction and must never be reported."""
    assert replicated_rows("|a|b|c|\n|---|---|---|\n|1|2|3|") == []


def test_non_spec_columns_are_never_compared():
    """Repeated values outside MIN/TYP/MAX are ordinary data, not a fault.

    A first version of this check compared every column and produced 28 hits
    on one datasheet, all legitimate: a resolution of "16 (16)" really is the
    same at every gain, and block-protection tables really are mostly zeros.
    """
    gains = (
        "|**Data rate**|**Gain 1**|**Gain 2**|**Gain 4**|\n"
        "|---|---|---|---|\n"
        "|2.5|16 (16)|16 (16)|16 (16)|"
    )
    assert replicated_rows(gains) == []

    bits = "|**BP2**|**BP1**|**BP0**|**Protected**|\n|---|---|---|---|\n|0|0|0|None|"
    assert replicated_rows(bits) == []


def test_a_spec_table_needs_three_columns_to_be_judged():
    """With MIN and TYP merged there are too few columns to call it a fault."""
    merged = (
        "|**PARAMETER**|**MIN**<br>**TYP**|**MAX**|\n"
        "|---|---|---|\n"
        "|Supply voltage|1.65|1.65|"
    )
    assert replicated_rows(merged) == []


def test_empty_cells_do_not_count_as_repetition():
    assert replicated_rows("|A|B|C|D|\n|---|---|---|---|\n|1||||") == []


def test_row_count_excludes_separators_and_prose():
    text = "some prose\n\n|a|b|\n|---|---|\n|1|2|\n|3|4|\n\nmore prose"
    assert table_row_count(text) == 3  # header + two data rows


def test_audit_reports_pages_of_suspect_rows():
    pages = [
        {"page_num": 4, "text": HONEST},
        {"page_num": 7, "text": FABRICATED},
    ]
    report = audit(pages)
    assert report["table_rows"] > 0
    assert [entry["page"] for entry in report["suspect_rows"]] == [7]


def test_audit_of_table_free_pages_is_clean():
    report = audit([{"page_num": 1, "text": "just prose, no tables here"}])
    assert report == {"table_rows": 0, "suspect_rows": []}
