"""Tests for heading detection and chunk selection.

These cover the three failures that cost the corpus 72% of its extracted text:
an exclusive allowlist, two heading lists that had to agree and did not, and a
vocabulary that only knew one vendor's wording.
"""
from __future__ import annotations

import pytest

from pcb_tool_pdf_extract.chunking import (
    Chunk,
    ESSENTIAL_HEADINGS,
    GENERAL,
    HEADING_RULES,
    chunk_pages,
    detect_heading_hint,
    strip_markdown,
    strip_markdown,
    HEADING_TIERS,
    heading_tier,
    looks_like_heading,
)
from pcb_tool_pdf_extract.extract_profile import select_chunks


# --- heading vocabulary -----------------------------------------------------

@pytest.mark.parametrize(
    "heading, expected",
    [
        # ST wording - what the original vocabulary was built from.
        ("Pin description", "pin description"),
        # TI wording - the omission that cost ADS114S06B its entire pinout.
        ("7.3 Pin Configuration and Functions", "pin description"),
        ("Terminal Functions", "pin description"),
        ("Signal Descriptions", "pin description"),
        ("PIN ASSIGNMENT", "pin description"),
        ("Package Pinout", "pin description"),
        ("Absolute Maximum Ratings", "absolute maximum ratings"),
        ("6.3 Recommended Operating Conditions", "recommended operating conditions"),
        ("Electrical Characteristics", "electrical characteristics"),
        ("DC Characteristics", "electrical characteristics"),
        ("AC Electrical Specifications", "electrical characteristics"),
        ("Register Map", "register map"),
        ("Detailed Register Descriptions", "register description"),
        ("Switching Characteristics", "timing characteristics"),
        ("Detailed Description", "functional description"),
        ("Device Functional Modes", "functional description"),
        ("Theory of Operation", "functional description"),
        ("Application and Implementation", "application information"),
        ("Layout Guidelines", "layout guidelines"),
        ("Power Supply Recommendations", "power supply recommendations"),
        ("Thermal Information", "thermal information"),
        ("Typical Characteristics", "typical characteristics"),
        ("Ordering Information", "package information"),
    ],
)
def test_vendor_headings_map_to_canonical_names(heading: str, expected: str):
    assert detect_heading_hint(f"{heading}\n\nsome body text follows here") == expected


def test_heading_is_found_below_the_top_of_the_page():
    """The old detector read only the first 20 lines, so mid-page sections -
    which is most of them in a two-column datasheet - were invisible."""
    page = "\n".join([f"body line {i}" for i in range(40)] + ["Pin Configuration and Functions"])
    assert detect_heading_hint(page) == "pin description"


def test_prose_mentioning_a_section_is_not_a_heading():
    page = (
        "The device is characterised over the range given in the Electrical "
        "Characteristics table on page 12, which should be consulted before "
        "selecting external components for the application circuit shown."
    )
    assert detect_heading_hint(page) == GENERAL


def test_looks_like_heading_rejects_sentences():
    assert looks_like_heading("7.3.1 Pin Configuration and Functions")
    assert looks_like_heading("Absolute Maximum Ratings")
    assert not looks_like_heading("See the pin description for details.")
    assert not looks_like_heading("x" * 200)


def test_every_rule_has_a_tier_and_essentials_are_tier_one():
    for rule in HEADING_RULES:
        assert heading_tier(rule.name) == rule.tier
    assert "pin description" in ESSENTIAL_HEADINGS
    assert heading_tier(GENERAL) == 3
    assert heading_tier("something unknown") == 3


# --- selection --------------------------------------------------------------

def _chunk(index: int, hint: str, size: int = 100) -> Chunk:
    return Chunk(
        chunk_id=f"chunk_{index:03d}",
        page_start=index,
        page_end=index,
        heading_hint=hint,
        text="x" * size,
    )


def test_selection_sends_everything_when_it_fits():
    """The core fix: selection is additive, not an allowlist.

    A chunk hinted 'general' is ordinary datasheet prose, and dropping it was
    what discarded 72% of the corpus - pin tables included.
    """
    chunks = [_chunk(1, GENERAL), _chunk(2, "pin description"), _chunk(3, GENERAL)]
    selection = select_chunks(chunks, max_chars=100_000)

    assert selection.chunks == chunks
    assert selection.dropped == []
    assert selection.complete
    assert selection.coverage == 1.0


def test_selection_keeps_document_order():
    chunks = [_chunk(1, GENERAL), _chunk(2, "pin description"), _chunk(3, "typical characteristics")]
    selected = select_chunks(chunks, max_chars=100_000).chunks
    assert [c.chunk_id for c in selected] == ["chunk_001", "chunk_002", "chunk_003"]


def test_budget_drops_lowest_tier_first():
    """When a datasheet will not fit, it should lose curves before pin tables."""
    chunks = [
        _chunk(1, "typical characteristics", 100),
        _chunk(2, "pin description", 100),
        _chunk(3, GENERAL, 100),
        _chunk(4, "electrical characteristics", 100),
    ]
    selection = select_chunks(chunks, max_chars=200)

    kept = {c.heading_hint for c in selection.chunks}
    assert kept == {"pin description", "electrical characteristics"}
    assert not selection.complete
    assert selection.coverage == pytest.approx(0.5)


def test_an_oversized_single_chunk_is_still_sent():
    """Better a too-large prompt that the API rejects loudly than an empty one."""
    selection = select_chunks([_chunk(1, GENERAL, 5000)], max_chars=100)
    assert len(selection.chunks) == 1


def test_empty_input_is_handled():
    selection = select_chunks([], max_chars=1000)
    assert selection.chunks == [] and selection.total_chars == 0
    assert selection.coverage == 1.0


# --- end to end over synthetic pages ---------------------------------------

def test_pin_section_survives_chunking_and_selection():
    """The M95P32 failure in miniature: pin numbers on an early page, and a
    pin-description section later, both of which must reach the model."""
    extracted = {
        "pages": [
            {"page_num": 1, "text": "Some Product\n\nfeatures list " + "x" * 500},
            {"page_num": 2, "text": "Connections Diagram\n\n1\nS\n2\nQ\n3\nW\n4\nVSS"},
            {"page_num": 3, "text": "Typical Characteristics\n\ncurves " + "x" * 500},
            {"page_num": 4, "text": "Pin Configuration and Functions\n\nfull table " + "x" * 500},
        ]
    }
    chunks = chunk_pages(extracted)
    selection = select_chunks(chunks, max_chars=100_000)

    sent = "\n".join(c.text for c in selection.chunks)
    assert "Connections Diagram" in sent
    assert "1\nS\n2\nQ" in sent, "the numbered pin table must reach the model"
    assert any(c.heading_hint == "pin description" for c in selection.chunks)


# --- markdown pages --------------------------------------------------------

def test_page_content_is_the_page_text():
    """Since 0.3.0 tables arrive inside the markdown, so a page is its text."""
    from pcb_tool_pdf_extract.chunking import page_content

    assert page_content({"page_num": 1, "text": "plain text"}) == "plain text"


def test_page_content_still_reads_legacy_step1_files():
    """0.2.0 wrote tables in a separate list; those files must still chunk."""
    from pcb_tool_pdf_extract.chunking import page_content

    page = {"page_num": 4, "text": "Pin Functions", "tables": ["| 1 | AINCOM |"]}
    content = page_content(page)
    assert content.startswith("Pin Functions")
    assert "| 1 | AINCOM |" in content


@pytest.mark.parametrize(
    "line, expected",
    [
        ("### **Pin Functions**", "pin description"),
        ("# **6 Pin Configuration and Functions**", "pin description"),
        ("## **7.1 Absolute Maximum Ratings**<sup>**(1)**</sup>",
         "absolute maximum ratings"),
        # Emphasis inside the phrase would split it without markdown stripping.
        ("**Pin** Configuration and Functions", "pin description"),
    ],
)
def test_markdown_headings_are_detected(line, expected):
    assert detect_heading_hint(line + "\n\nbody text") == expected


def test_strip_markdown_removes_decoration():
    assert strip_markdown("## **7.1 Absolute Maximum Ratings**<sup>**(1)**</sup>") == (
        "7.1 Absolute Maximum Ratings (1)"
    )


def test_markdown_table_rows_do_not_relabel_a_chunk():
    """A table cell reading "Pin Description" must not retitle the page."""
    extracted = {
        "pages": [{
            "page_num": 1,
            "text": ("## **Ordering Information**\n\n"
                     "|Pin Description|x|\n|---|---|\n|1|y|\n" + "x" * 200),
        }]
    }
    chunks = chunk_pages(extracted)
    assert chunks[0].heading_hint == "package information"


# --- extraction passes -------------------------------------------------------

from pcb_tool_pdf_extract.extract_profile import (  # noqa: E402
    IDENTITY_CHARS,
    PASSES,
    PASS_NAMES,
    get_pass,
    merge_profiles,
    select_for_pass,
)


def _doc(*specs) -> list[Chunk]:
    """Build chunks from (hint, size) pairs."""
    return [_chunk(i + 1, hint, size) for i, (hint, size) in enumerate(specs)]


def test_a_small_datasheet_reaches_every_pass_whole():
    """The split must cost small datasheets nothing.

    A 9-page MOSFET fits entirely, so every pass should still see all of it -
    targeting is only meant to bite when a document cannot be sent whole.
    """
    chunks = _doc(("general", 400), ("pin description", 400), ("register page", 400))
    for extraction_pass in PASSES:
        selection = select_for_pass(chunks, extraction_pass, max_chars=100_000)
        assert selection.chunks == chunks, extraction_pass.name
        assert selection.complete


def test_identity_preamble_is_always_included():
    """Every pass needs the front page, or it cannot name the part."""
    chunks = _doc(("general", 500), ("register page", 200_000), ("pin description", 200_000))
    selection = select_for_pass(chunks, get_pass("registers"), max_chars=210_000)
    assert chunks[0] in selection.chunks, "opening chunk carries part_number"


def test_a_pass_prioritises_its_own_sections_over_other_tier_one_sections():
    """Under budget, the registers pass keeps register pages, not pin tables."""
    chunks = _doc(("general", 100), ("pin description", 200_000), ("register page", 200_000))
    selection = select_for_pass(chunks, get_pass("registers"), max_chars=210_000)

    hints = {c.heading_hint for c in selection.chunks}
    assert "register page" in hints
    assert "pin description" not in hints

    # And the schematic pass makes the opposite choice on the same document.
    other = select_for_pass(chunks, get_pass("schematic"), max_chars=210_000)
    assert "pin description" in {c.heading_hint for c in other.chunks}
    assert "register page" not in {c.heading_hint for c in other.chunks}


def test_selection_is_returned_in_document_order():
    chunks = _doc(("general", 100), ("register page", 100), ("pin description", 100))
    selected = select_for_pass(chunks, get_pass("schematic"), max_chars=100_000).chunks
    assert [c.chunk_id for c in selected] == [c.chunk_id for c in chunks]


def test_merge_unions_the_sections_each_pass_owns():
    merged, notes = merge_profiles({
        "schematic": {"component": {"part_number": "ADS114S06B"},
                      "schematic": {"supply": [{"rail": "AVDD"}]}},
        "software": {"component": {"part_number": "ADS114S06B"},
                     "software": {"interface": {"protocol": "SPI"}}},
        "registers": {"software": {"registers": [{"addr": "0x00"}]}},
    })
    assert merged["component"]["part_number"] == "ADS114S06B"
    assert merged["schematic"]["supply"][0]["rail"] == "AVDD"
    assert merged["software"]["interface"]["protocol"] == "SPI"
    assert merged["software"]["registers"][0]["addr"] == "0x00"
    assert notes == []


def test_merge_reports_a_part_number_disagreement():
    """Two passes naming different parts means something is wrong upstream."""
    merged, notes = merge_profiles({
        "schematic": {"component": {"part_number": "ADS114S06B"}},
        "software": {"component": {"part_number": "ADS114S08B"}},
    })
    assert merged["component"]["part_number"] == "ADS114S06B", "schematic pass wins"
    assert len(notes) == 1 and "disagreed" in notes[0]
    assert "ADS114S08B" in notes[0]


def test_merge_tolerates_a_missing_pass():
    merged, notes = merge_profiles({"schematic": {"component": {"part_number": "BSS316N"}}})
    assert merged["component"]["part_number"] == "BSS316N"
    assert "software" not in merged
    assert notes == []


def test_every_pass_hint_is_a_real_heading_name():
    """A typo in a pass's hint set would silently select nothing."""
    known = set(HEADING_TIERS)
    for extraction_pass in PASSES:
        unknown = extraction_pass.hints - known
        assert not unknown, f"{extraction_pass.name}: {unknown}"


def test_passes_are_recommended_only_when_a_datasheet_will_not_fit():
    """Splitting a datasheet that fits costs 3x the input and gains nothing."""
    from pcb_tool_pdf_extract.extract_profile import recommend_passes

    fits = _doc(("general", 1_000), ("pin description", 1_000))
    assert recommend_passes(fits, max_chars=100_000) == []

    too_big = _doc(("general", 500_000), ("register page", 500_000))
    assert recommend_passes(too_big, max_chars=100_000) == list(PASS_NAMES)


def test_an_oversized_pass_does_not_top_up_with_other_sections():
    """Topping up regardless made three passes cost 3x a single prompt."""
    chunks = _doc(("general", 1_000), ("pin description", 90_000),
                  ("register page", 90_000), ("interface", 90_000))
    selection = select_for_pass(chunks, get_pass("schematic"), max_chars=150_000)

    hints = {c.heading_hint for c in selection.chunks}
    assert hints == {"general", "pin description"}, "no unrelated top-up"


# --- HAL-covered parts -------------------------------------------------------

def test_register_pass_is_skipped_for_a_microcontroller():
    """Firmware for an MCU is written against the vendor HAL, not the map."""
    from pcb_tool_pdf_extract.extract_profile import skip_registers_reason

    reason = skip_registers_reason({"component": {"part_number": "RP2350",
                                                  "category": "mcu"}})
    assert reason and "HAL" in reason


def test_register_pass_runs_for_a_peripheral():
    """The ADS114S06B's registers are the contract a driver codes against."""
    from pcb_tool_pdf_extract.extract_profile import skip_registers_reason

    for category in ("sensor", "interface", "power", "memory", ""):
        assert skip_registers_reason(
            {"component": {"part_number": "X", "category": category}}
        ) is None, category


def test_an_unknown_category_does_not_skip():
    """Absent a category, extract the registers - omitting them is the
    lossy choice, and a peripheral is the more common case."""
    from pcb_tool_pdf_extract.extract_profile import skip_registers_reason

    assert skip_registers_reason({}) is None
    assert skip_registers_reason({"component": {}}) is None


def test_category_matching_is_case_and_space_insensitive():
    from pcb_tool_pdf_extract.extract_profile import skip_registers_reason

    assert skip_registers_reason({"component": {"category": "  MCU "}})
    assert skip_registers_reason({"component": {"category": "SoC"}})
