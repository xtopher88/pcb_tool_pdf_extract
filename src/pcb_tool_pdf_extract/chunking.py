"""Splitting extracted page text into heading-aware chunks.

Heading detection has one job: label each chunk so that, when a datasheet is
too large to send whole, the least useful parts are the ones dropped. It is an
ordering signal, never a filter - `extract_profile.select_chunks` decides what
is sent, and it sends everything that fits.

Two things this vocabulary has to get right:

- **Vendor wording differs.** ST writes "Pin description", TI writes "Pin
  Configuration and Functions", others write "Terminal Functions" or "Signal
  Descriptions". A vocabulary built from one vendor's datasheets silently
  mislabels every other vendor's, and a mislabelled pin section is the one
  that costs most.
- **A heading is a line, not a phrase in a paragraph.** "see the Electrical
  Characteristics table" must not label a page. Matches are therefore required
  to look like headings: short lines, optionally section-numbered, not prose
  that happens to mention the words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
import json
from pathlib import Path

GENERAL = "general"


@dataclass(frozen=True)
class HeadingRule:
    """One canonical section name and the wordings that mean it.

    `tier` orders chunks when a datasheet will not fit in one prompt: tier 1
    is what a component profile is built from, tier 2 supports it, tier 3 is
    everything else.
    """

    name: str
    tier: int
    pattern: re.Pattern[str]


def _rule(name: str, tier: int, *alternatives: str) -> HeadingRule:
    return HeadingRule(name, tier, re.compile("|".join(alternatives), re.IGNORECASE))


#: Ordered: the first rule that matches a heading line wins, so the more
#: specific wordings come first.
HEADING_RULES: tuple[HeadingRule, ...] = (
    _rule(
        "pin description", 1,
        r"pin\s+configurations?\s+and\s+functions?",
        r"pin\s+(descriptions?|definitions?|configurations?|assignments?|functions?|list|outs?)",
        r"terminal\s+(configurations?|functions?|descriptions?|assignments?)",
        r"package\s+pinouts?",
        r"signal\s+descriptions?",
        r"pad\s+(descriptions?|assignments?)",
        r"connections?\s+(diagram|checklist)",
        r"\bpinout\b",
    ),
    _rule(
        "absolute maximum ratings", 1,
        r"absolute\s+maximum\s+ratings?",
        r"\bstress\s+ratings?\b",
    ),
    _rule(
        "recommended operating conditions", 1,
        r"recommended\s+operating\s+(conditions|ratings|range)",
        r"operating\s+conditions",
    ),
    _rule(
        "electrical characteristics", 1,
        r"electrical\s+(characteristics|specifications?)",
        r"\b(dc|ac)\s+(electrical\s+)?(characteristics|specifications?|parameters)",
        r"static\s+characteristics",
        r"dynamic\s+characteristics",
    ),
    _rule(
        "register map", 1,
        r"register\s+(maps?|summary|listings?|table)",
        r"memory\s+(map|organization)",
    ),
    _rule(
        "register description", 1,
        r"register\s+(descriptions?|definitions?|details?)",
        r"detailed\s+register\s+descriptions?",
    ),
    # One page per register, headed "PERIPHERAL: NAME Register". Large SoC
    # datasheets are mostly these - 938K characters of RP2350, 498K of RP2040 -
    # and without a rule they all land in `general`, where they are both
    # invisible to selection and large enough to crowd out everything else.
    # Kept separate from "register description" so a pass can take the prose
    # that explains a peripheral without dragging in every register page.
    _rule(
        "register page", 1,
        r"^[A-Z][A-Z0-9_]{1,24}\s*:\s*\S.*\bregisters?\s*$",
    ),
    _rule(
        "timing characteristics", 2,
        r"timing\s+(characteristics|requirements|specifications?|parameters|diagrams?)",
        r"switching\s+characteristics",
    ),
    _rule(
        "interface", 2,
        r"(i2c|i²c|iic)\b[^\n]{0,24}(interface|bus|protocol)",
        r"(spi|serial\s+peripheral)\b[^\n]{0,24}(interface|bus|protocol)",
        r"\buart\b[^\n]{0,24}(interface|protocol)",
        r"serial\s+(interface|communication|protocol)",
        r"communication\s+(interface|protocol)",
        r"instruction\s+set",
        r"command\s+(set|table|descriptions?)",
    ),
    _rule(
        "functional description", 2,
        r"(detailed\s+)?functional\s+descriptions?",
        r"device\s+functional\s+modes",
        r"theory\s+of\s+operation",
        r"detailed\s+description",
        r"principle\s+of\s+operation",
    ),
    _rule(
        "power supply recommendations", 2,
        r"power\s+supply\s+(recommendations?|considerations?|requirements?|design)",
        r"power(\s+up|\s+on|-up|-on)\s+(sequence|requirements?|behaviou?r)",
        r"supply\s+(requirements?|sequencing)",
    ),
    _rule(
        "application information", 2,
        r"application\s+(information|and\s+implementation|circuits?|notes?|examples?)",
        r"typical\s+applications?",
        r"design\s+(procedure|requirements?|examples?)",
    ),
    _rule(
        "layout guidelines", 2,
        r"layout\s+(guidelines?|considerations?|recommendations?|examples?)",
        r"pcb\s+layout",
        r"board\s+layout",
    ),
    _rule(
        "thermal information", 2,
        r"thermal\s+(information|characteristics|considerations?|resistance|data)",
        r"power\s+dissipation",
    ),
    _rule(
        "typical characteristics", 3,
        r"typical\s+(performance\s+)?characteristics",
        r"typical\s+performance\s+curves",
        r"characteristic\s+curves",
    ),
    _rule(
        "package information", 3,
        r"package\s+(information|outlines?|dimensions|drawings?|mechanical)",
        r"mechanical\s+(data|information|drawings?)",
        r"(ordering|order)\s+information",
        r"tape\s+and\s+reel",
        r"revision\s+history",
    ),
)

#: Tier for every canonical name, plus the catch-all.
HEADING_TIERS: dict[str, int] = {rule.name: rule.tier for rule in HEADING_RULES}
HEADING_TIERS[GENERAL] = 3

#: Canonical names that a component profile is primarily built from. Used by
#: the tests that assert coverage, not by selection.
ESSENTIAL_HEADINGS = tuple(rule.name for rule in HEADING_RULES if rule.tier == 1)

#: Leading section numbering to strip before judging a line: "7.3.1 Pin ..."
_SECTION_NUMBER = re.compile(r"^\s*(\d+(\.\d+)*|[IVXLC]+\.|[A-Z]\.)\s+")

#: A heading line is short. Longer lines are prose that mentions the words.
_MAX_HEADING_LEN = 80


@dataclass
class Chunk:
    chunk_id: str
    page_start: int
    page_end: int
    heading_hint: str
    text: str


#: Markdown decoration around a heading: "## **7.1 Absolute Maximum
#: Ratings**<sup>**(1)**</sup>". Since 0.3.0 pages arrive as markdown, so this
#: is stripped before matching - otherwise emphasis inside a phrase
#: ("**Pin** Configuration") could split it and defeat the match.
#:
#: Underscores are deliberately NOT stripped. They are emphasis in markdown but
#: they are also part of the names this vocabulary matches on - stripping them
#: turned "IO_BANK0: ... Register" into "IO BANK0: ... Register" and defeated
#: the register-page rule.
_MARKDOWN_NOISE = re.compile(r"<[^>]{1,20}>|[*`#]+")

#: A markdown heading line. Where a page has these they are authoritative, and
#: prose is not consulted at all - which keeps a sentence like "the CTRL
#: register: see the register description" from labelling a page.
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+\S")


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def strip_markdown(line: str) -> str:
    """A heading line with its markdown decoration removed."""
    return re.sub(r"\s+", " ", _MARKDOWN_NOISE.sub(" ", line)).strip()


def looks_like_heading(line: str) -> bool:
    """True when a line is plausibly a section heading rather than prose.

    Datasheet headings are short, often numbered, and rarely end in a full
    stop. Requiring that shape is what lets the whole page be scanned without
    every cross-reference labelling a chunk.
    """
    stripped = strip_markdown(line)
    if not stripped or len(stripped) > _MAX_HEADING_LEN:
        return False
    body = _SECTION_NUMBER.sub("", stripped)
    if not body:
        return False
    # Prose ends in punctuation; headings almost never do.
    if body.endswith((".", ",", ";", ":")) and not body.endswith("..."):
        return False
    # A heading is mostly its own title, not a sentence containing one.
    words = body.split()
    if len(words) > 9:
        return False
    return True


def detect_heading_hint(page_text: str) -> str:
    """Canonical section name for a page, or GENERAL.

    Scans every line. The previous implementation looked only at the first 20
    lines of a page, so a section starting mid-page - which is most of them in
    a two-column datasheet - was invisible.
    """
    lines = page_text.splitlines()
    marked = [line for line in lines if _MARKDOWN_HEADING.match(line)]

    # Markdown headings first. Only a page without any is judged on prose,
    # which is what a docling-extracted page looks like.
    for candidates in (marked, lines):
        if not candidates:
            continue
        for raw_line in candidates:
            if not looks_like_heading(raw_line):
                continue
            line = strip_markdown(raw_line)
            for rule in HEADING_RULES:
                if rule.pattern.search(line):
                    return rule.name
        if candidates is marked:
            # The page had headings and none matched; trusting prose here
            # would only add noise.
            return GENERAL
    return GENERAL


def heading_tier(name: str) -> int:
    """Selection tier for a canonical heading name; unknown names sort last."""
    return HEADING_TIERS.get(name, 3)


def page_content(page: dict) -> str:
    """A page's content as step 1 recorded it.

    Since 0.3.0 the extractor emits markdown with tables already rendered in
    place, so a page is just its text. Step-1 files written by 0.2.0 carry a
    separate `tables` list, which is appended here so they still chunk.
    """
    text = page.get("text", "")
    legacy_tables = page.get("tables") or []
    if not legacy_tables:
        return text
    return text + "\n\n" + "\n\n".join(legacy_tables)


def chunk_pages(extracted: dict, max_chars: int = 12000) -> list[Chunk]:
    """Split extracted pages into chunks, starting a new one at each heading."""
    chunks: list[Chunk] = []
    current_text: list[str] = []
    page_start = 1
    page_end = 1
    current_heading = GENERAL
    idx = 1

    for page in extracted["pages"]:
        if not page.get("text"):
            continue
        # Headings come from the page's own text; appended table markdown has
        # no headings and would only add noise to the match.
        heading = detect_heading_hint(page["text"])
        text = page_content(page)
        proposed = "\n\n".join(current_text + [f"[Page {page['page_num']}]\n{text}"])

        should_flush = (
            len(proposed) > max_chars
            or (heading != GENERAL and current_text)
        )

        if should_flush:
            chunks.append(Chunk(
                chunk_id=f"chunk_{idx:03d}",
                page_start=page_start,
                page_end=page_end,
                heading_hint=current_heading,
                text="\n\n".join(current_text),
            ))
            idx += 1
            current_text = []
            page_start = page["page_num"]
            current_heading = heading

        if not current_text:
            page_start = page["page_num"]
            current_heading = heading

        current_text.append(f"[Page {page['page_num']}]\n{text}")
        page_end = page["page_num"]

    if current_text:
        chunks.append(Chunk(
            chunk_id=f"chunk_{idx:03d}",
            page_start=page_start,
            page_end=page_end,
            heading_hint=current_heading,
            text="\n\n".join(current_text),
        ))

    return chunks


def save_chunks(chunks: list[Chunk], out_path: Path) -> None:
    payload = [asdict(c) for c in chunks]
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
