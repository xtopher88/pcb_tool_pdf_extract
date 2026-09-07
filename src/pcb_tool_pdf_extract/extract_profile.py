from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .chunking import Chunk, heading_tier
from .validate_profile import validate_profile


def load_system_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


#: Character budget for the datasheet excerpts in one prompt.
#:
#: Sized against a 200K-token context. Datasheet text is token-dense - tables
#: of numbers and part codes run nearer three characters per token than the
#: usual four - so this is budgeted at the pessimistic ratio: 320K chars is
#: about 107K tokens, leaving room for the schema (~10K) and the response
#: budget (max_tokens 64K, shared with thinking) inside 200K.
#:
#: In practice this binds on almost nothing. Of a 17-datasheet corpus only the
#: 642-page RP2040 exceeds it; the next largest is 238K chars and goes whole.
#: Raise it with DATASHEET_MAX_CHARS if a model with a larger context is in use.
DEFAULT_MAX_CHARS = 320_000


@dataclass
class Selection:
    """What was sent to the model, and what would not fit."""

    chunks: list[Chunk]
    dropped: list[Chunk]
    included_chars: int
    total_chars: int

    @property
    def complete(self) -> bool:
        return not self.dropped

    @property
    def coverage(self) -> float:
        return self.included_chars / self.total_chars if self.total_chars else 1.0


def select_chunks(chunks: list[Chunk], max_chars: int = DEFAULT_MAX_CHARS) -> Selection:
    """Choose which chunks to send, keeping as much of the datasheet as fits.

    Selection is additive: everything is sent unless the budget forbids it.
    Heading hints only decide *what is dropped first* when a datasheet is too
    large - they never exclude a chunk that would otherwise fit.

    This matters more than it sounds. The previous behaviour kept only chunks
    whose heading was on an allowlist, which sent 28% of a 17-datasheet corpus
    and silently discarded pin tables whose headings used a wording the list
    did not know. A page that never reaches the model cannot be extracted from,
    and nothing downstream could tell that from a datasheet that genuinely
    lacked the section.

    Chunks are chosen by tier but returned in document order, so the excerpt
    still reads as a datasheet rather than as a pile of sections.
    """
    total = sum(len(chunk.text) for chunk in chunks)
    if not chunks:
        return Selection([], [], 0, 0)

    # Tier first, then document order, so an over-budget datasheet loses its
    # least useful sections rather than its last ones.
    order = sorted(
        range(len(chunks)),
        key=lambda i: (heading_tier(chunks[i].heading_hint), i),
    )

    kept: set[int] = set()
    used = 0
    for index in order:
        size = len(chunks[index].text)
        if used + size > max_chars and kept:
            continue
        kept.add(index)
        used += size

    selected = [chunk for i, chunk in enumerate(chunks) if i in kept]
    dropped = [chunk for i, chunk in enumerate(chunks) if i not in kept]
    return Selection(selected, dropped, used, total)


def select_relevant_chunks(
    chunks: list[Chunk], max_chars: int = DEFAULT_MAX_CHARS
) -> list[Chunk]:
    """Chunks to send, in document order."""
    return select_chunks(chunks, max_chars).chunks


# --- extraction passes ------------------------------------------------------

#: Characters of the document's opening reserved for identity in every pass.
#: `component.part_number`, the manufacturer and the description live on the
#: front page, and a pass that cannot name the part it is describing produces
#: a profile that cannot be filed.
IDENTITY_CHARS = 8_000


@dataclass(frozen=True)
class ExtractionPass:
    """One targeted extraction over the sections it needs.

    A 1,380-page SoC and a 9-page MOSFET do not fit the same prompt. The
    sections a profile is built from are small and disjoint - for RP2350 the
    schematic-facing ones total about 150K characters, under 5% of the
    document - so sending each pass only its own sections makes the large
    datasheets tractable and costs the small ones nothing.
    """

    name: str
    hints: frozenset[str]
    produces: tuple[str, ...]
    purpose: str


PASSES: tuple[ExtractionPass, ...] = (
    ExtractionPass(
        name="schematic",
        hints=frozenset({
            "pin description",
            "absolute maximum ratings",
            "recommended operating conditions",
            "electrical characteristics",
            "power supply recommendations",
            "thermal information",
            "layout guidelines",
            "functional description",
        }),
        produces=("component", "schematic"),
        purpose="identity, pinout, supplies, ratings and design checks",
    ),
    ExtractionPass(
        name="software",
        hints=frozenset({
            "interface",
            "register map",
            "register description",
            "timing characteristics",
        }),
        produces=("component", "software"),
        purpose="protocol, addressing, interrupts and init sequence",
    ),
    ExtractionPass(
        name="registers",
        hints=frozenset({"register page", "register map", "register description"}),
        produces=("software",),
        purpose="the register map itself",
    ),
)

PASS_NAMES = tuple(p.name for p in PASSES)


def get_pass(name: str) -> ExtractionPass:
    for extraction_pass in PASSES:
        if extraction_pass.name == name:
            return extraction_pass
    raise ValueError(f"Unknown pass: {name!r}. Choose from {', '.join(PASS_NAMES)}.")


#: Profile categories whose register map a vendor HAL already covers.
#: Firmware for these is written against STM32Cube, the Pico SDK or similar,
#: not against the register map directly.
HAL_COVERED_CATEGORIES = frozenset({"mcu", "processor", "soc", "fpga"})


def skip_registers_reason(profile: dict) -> str | None:
    """Why the register pass should be skipped for this part, or None.

    Peripheral ICs are what drivers get written for, so their register maps
    earn their place: the ADS114S06B's 18 registers are the contract firmware
    codes against. A microcontroller's are a different matter - nobody writes
    to STM32 or RP2350 registers directly when the vendor ships a HAL, and the
    map is enormous (RP2350's register documentation is 1.43M characters, of
    which a single prompt reaches 22%). Extracting a fifth of a register map
    nobody would use is worse than not extracting it: it looks complete.
    """
    category = str((profile.get("component") or {}).get("category", "")).strip().lower()
    if category in HAL_COVERED_CATEGORIES:
        return (
            f"category '{category}' is covered by a vendor HAL; register-level "
            "extraction skipped in favour of the vendor's own headers"
        )
    return None


def recommend_passes(chunks: list[Chunk], max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Pass names worth running, or [] to send one prompt instead.

    Measured: for a datasheet that fits in one prompt, three passes cost
    exactly three times the input and gain nothing, because each pass is sent
    the whole document anyway. For one that does not fit, they cost about
    1.6x and take the schematic and software sections from a tenth of the
    document to all of it. So the split is worth running precisely when the
    document is too large to send whole.
    """
    total = sum(len(chunk.text) for chunk in chunks)
    return [] if total <= max_chars else list(PASS_NAMES)


def select_for_pass(
    chunks: list[Chunk],
    extraction_pass: ExtractionPass,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Selection:
    """Chunks for one pass: its own sections, plus identity, plus what fits.

    Three layers, in priority order:

    1. **Identity.** The opening of the document, always, so every pass knows
       which part it is describing.
    2. **The pass's sections**, by heading hint.
    3. **Whatever else fits**, in tier order. This is what makes the split
       free for small datasheets: a 9-page MOSFET is still sent whole to every
       pass, exactly as it is today. The targeting only bites once a document
       is too large to send entirely, which is precisely when it is needed.
    """
    if not chunks:
        return Selection([], [], 0, 0)

    total = sum(len(chunk.text) for chunk in chunks)
    kept: set[int] = set()
    used = 0

    def take(index: int) -> None:
        nonlocal used
        if index in kept:
            return
        size = len(chunks[index].text)
        if used + size > max_chars and kept:
            return
        kept.add(index)
        used += size

    # 1. Identity preamble. The first chunk is taken whatever its size - it is
    #    the front page, and there is nowhere else to learn the part number.
    #    After that the preamble stops before exceeding its allowance, so a
    #    large second chunk cannot be swallowed as "identity" and spend the
    #    budget the pass needs for its own sections.
    preamble = 0
    for index, chunk in enumerate(chunks):
        size = len(chunk.text)
        if index > 0 and preamble + size > IDENTITY_CHARS:
            break
        take(index)
        preamble += size

    # 2. The pass's own sections, in document order.
    for index, chunk in enumerate(chunks):
        if chunk.heading_hint in extraction_pass.hints:
            take(index)

    # 3. Top up with everything else - but only when the whole document fits.
    #
    #    Topping up unconditionally made every pass fill the budget, so three
    #    passes cost three times a single prompt while each still dragged in
    #    ~200K characters of another pass's material. Filling only when there
    #    is room for the entire document keeps the split lossless for small
    #    datasheets (they are sent whole to every pass, exactly as before)
    #    without paying for noise on the large ones, which is the only case
    #    the split exists to serve.
    if total <= max_chars:
        for index in sorted(range(len(chunks)),
                            key=lambda i: (heading_tier(chunks[i].heading_hint), i)):
            take(index)

    selected = [chunk for i, chunk in enumerate(chunks) if i in kept]
    dropped = [chunk for i, chunk in enumerate(chunks) if i not in kept]
    return Selection(selected, dropped, used, total)


def merge_profiles(by_pass: dict[str, dict]) -> tuple[dict, list[str]]:
    """Combine per-pass results into one profile, reporting disagreements.

    Each pass owns the keys it `produces`, so the merge is mostly a union.
    `component` is the exception: every pass sees the identity preamble and so
    every pass has an opinion. The schematic pass wins, because it is the one
    that also reads the pinout and package sections, but a disagreement about
    the part number is reported rather than silently resolved - two passes
    naming different parts means something is wrong with the inputs.
    """
    merged: dict = {}
    notes: list[str] = []

    ordered = [p.name for p in PASSES if p.name in by_pass]

    for name in ordered:
        result = by_pass[name] or {}
        extraction_pass = get_pass(name)
        for key in extraction_pass.produces:
            value = result.get(key)
            if not isinstance(value, dict):
                continue
            if key not in merged:
                merged[key] = dict(value)
                continue
            for sub_key, sub_value in value.items():
                if sub_key not in merged[key]:
                    merged[key][sub_key] = sub_value

    # Part numbers must agree; the schematic pass is authoritative.
    seen: dict[str, str] = {}
    for name in ordered:
        part = ((by_pass[name] or {}).get("component") or {}).get("part_number")
        if isinstance(part, str) and part.strip():
            seen[name] = part.strip()
    distinct = set(seen.values())
    if len(distinct) > 1:
        authoritative = seen.get("schematic") or next(iter(seen.values()))
        merged.setdefault("component", {})["part_number"] = authoritative
        disagreement = ", ".join(f"{n}={v!r}" for n, v in sorted(seen.items()))
        notes.append(
            f"passes disagreed on component.part_number ({disagreement}); "
            f"kept {authoritative!r} from the schematic pass"
        )

    return merged, notes


def build_user_prompt(
    datasheet_file: str,
    selected_chunks: list[Chunk],
    schema_markdown: str,
) -> str:
    chunk_text = "\n\n".join(
        f"## {c.chunk_id} pages {c.page_start}-{c.page_end} ({c.heading_hint})\n{c.text}"
        for c in selected_chunks
    )

    return f"""
You are extracting a component profile from a datasheet into YAML.

Requirements:
- Output valid YAML only.
- Follow the schema exactly.
- Do not invent facts not supported by the datasheet text.
- Omit optional fields when unknown.
- Use lowercase hex with 0x prefix.
- Use exact pin names and register names from the datasheet.
- Add concise notes where the datasheet imposes a design constraint.
- If a field is uncertain, omit it instead of guessing.

Schema:
{schema_markdown}

Datasheet filename:
{datasheet_file}

Datasheet excerpts:
{chunk_text}
""".strip()


def parse_yaml_response(text: str) -> dict:
    import yaml

    # Prefer extracting content from a markdown code fence if present
    fence_match = re.search(r"```[a-zA-Z]*\s*\n(.*?)\n```", text.strip(), re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()
    else:
        stripped = text.strip()
    result = yaml.safe_load(stripped)
    if not isinstance(result, dict):
        raise ValueError(
            f"LLM response did not parse as a YAML dict (got {type(result).__name__}).\n"
            f"First 300 chars of response:\n{text[:300]}"
        )
    return result


def prepare_user_prompt(
    datasheet_file: str,
    selected_chunks: list[Chunk],
    schema_markdown: str,
) -> str:
    """Build the user prompt, refusing datasheets with no usable text layer.

    Split out from extract_and_validate so the batch path can prepare every
    prompt up front - and fail fast on unreadable PDFs - before spending
    anything on a submission.
    """
    total_chars = sum(len(c.text) for c in selected_chunks)
    if not selected_chunks or total_chars < 150:
        raise ValueError(
            f"Insufficient text extracted from {datasheet_file} ({total_chars} chars). "
            "The PDF may be image-only or contain no selectable text. "
            "Try --extractor docling (or auto)."
        )
    return build_user_prompt(
        datasheet_file=datasheet_file,
        selected_chunks=selected_chunks,
        schema_markdown=schema_markdown,
    )


def parse_and_validate(response_text: str) -> tuple[dict, list[str]]:
    """Turn a raw model response into a validated profile plus its issue list.

    Shared by the streaming and batch paths so both apply the same checks.
    """
    profile = parse_yaml_response(response_text)
    result = validate_profile(profile)

    issues = []
    if not result.ok:
        issues.extend(result.errors)
    issues.extend(result.warnings)
    return profile, issues


def extract_and_validate(
    llm_client,
    system_prompt: str,
    datasheet_file: str,
    selected_chunks: list[Chunk],
    schema_markdown: str,
) -> tuple[dict, list[str]]:
    user_prompt = prepare_user_prompt(
        datasheet_file=datasheet_file,
        selected_chunks=selected_chunks,
        schema_markdown=schema_markdown,
    )
    response = llm_client.generate_yaml_profile(system_prompt, user_prompt)
    return parse_and_validate(response)
