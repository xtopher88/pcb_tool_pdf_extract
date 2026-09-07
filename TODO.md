# TODO — pcb_tool_pdf_extract

Open items as of 2026-09-07, with the evidence behind each so they can be
picked up cold. The 2026-09-06/07 work (rename, text-loss fix, pymupdf4llm
swap, extraction passes) is committed as of `59373bd`. Design narrative and history live in
[`pdf-schematic-extraction-plan.md`](pdf-schematic-extraction-plan.md); this is
the actionable list.

---

## 1. Do first

### 1.1 Regenerate every profile

**Every profile is stale.** Measured against the current `pdf_step1`:

| State | Count |
|---|---|
| current | **0** |
| stale (produced by extractor 0.1.0) | 15 |
| no profile yet | 3 (`9e56b777…`, `a84bd8d5…`, RP2350) |

Extraction changed three times (0.1.0 → 0.2.0 → 0.3.0), so every
`step1_content_sha256` differs from what the profiles were built on. Step 1
and step 2 are already done for all 18 PDFs in `pdf_input/`; only step 3 is
outstanding. Use `batch` for the 50% rate.

**Check these three first** — they are the gaps that drove the whole
extraction rework, and they are the cheapest way to confirm it worked:

- **ADS114S06B** — had no `schematic.pinout` at all, because TI's "Pin
  Configuration and Functions" matched no heading. That section now reaches
  the prompt.
- **M95P32-I** — pin *names* only, no numbers. `pymupdf4llm` now surfaces
  "Table 1. Signal names" (pad number → signal name) in full. If this comes
  through, the `pinmap.yaml` override in `pcb_board_kiln_controller` becomes
  unnecessary — delete it and re-run `net-context` to confirm 8/8 pins still
  resolve.
- **ST1L05** — pinout merged the DFN6 and DFN8 variants into 9 entries for a
  6-pin part.

### 1.2 Re-validate after regeneration

7 of 15 profiles fail schema validation today:

| Profile | Failure |
|---|---|
| ADS114S06B | missing `schematic.pinout` |
| gqrelays | missing `schematic.pinout` |
| 100, 2d53d8d8…, BSS316N | missing `schematic.supply` |
| RP2040 | missing `schematic.decoupling` |
| ST7567 | `component.package` is required |

Some should resolve on regeneration; some will not, because the rules
themselves are wrong - see §2. Three of these seven fail only because
`passive` is required to have `schematic.supply`. Fix §2 before reading much
into a re-validation run.

---

## 2. Part taxonomy and category-driven schema

A category-driven schema already exists - `schema_rules.REQUIRED_BY_CATEGORY`
maps category to required sections, the schema doc carries the same table, and
the model emits `component.category`, which is already acted on (the register
pass reads it). What is missing is not the mechanism but a taxonomy that fits
the parts, and rules that are true.

This is the root of several of the validation failures in §1.2, which is why
it comes before the rest.

### 2.1 The taxonomy is too coarse

Six categories exist - `sensor`, `power`, `mcu`, `mux`, `interface`,
`passive` - and `passive` has become a dumping ground for everything that is
not an IC:

| Part | What it is | Filed as |
|---|---|---|
| `100` | E-Switch SPDT toggle switch | `passive` |
| `2d53d8d8...` | USB-C connector drawing | `passive` |
| `BSS316N` | MOSFET | `passive` |
| `PEC12R` | Rotary encoder | `passive` |
| `M95P32-I`, `W25Q16JV` | EEPROM / flash | `interface` |

None of the first four is passive in the electronics sense, and the last two
are memory. The model picked the least-wrong option available to it.

Missing categories, at least: `discrete` (MOSFET, diode, transistor),
`connector`, `electromechanical` (switch, encoder, relay), `memory`,
`display`. Each needs a required-sections row reflecting what such parts
actually have.

### 2.2 The required sections are wrong for `passive`

`passive` requires `schematic.supply`. **That alone causes 3 of the 7
validation failures** (`100`, `2d53d8d8...`, `BSS316N`). A connector, a toggle
switch and a MOSFET have no supply rail, so the requirement can never be
satisfied - validation is reporting a schema defect as a data defect.

Fix the table alongside 2.1; the two are one change.

### 2.3 Distinguish "missing" from "not applicable"

The more important half. Today an absent section fails validation identically
whether extraction lost it or the datasheet never had it. BSS316N genuinely
has no supply section - saying so is a valid answer, and materially different
from having missed one.

Sections should be *expected* per category, satisfiable either by content or
by an explicit `not_in_datasheet` marker. This is the same distinction the
rest of the pipeline already makes: truncation is reported, withheld tables
are recorded with a reason, skipped passes are named in `.meta.json`.

### 2.4 Narrow the schema shown to the model

Every prompt currently carries all **14,150 characters** of the schema - every
category's rules - regardless of the part or the pass. Showing only the
relevant category's contract would sharpen the prompt and cost nothing. The
pass already appends its own instructions; the category could narrow the
schema the same way.

### 2.5 Add a cheap identify pre-pass

Category is decided *during* extraction, which is too late to shape it. A
front-page-only pass (~8K characters, the identity preamble that every pass
already receives) returning just part number, manufacturer and category would
let everything downstream be category-aware:

- the model sees only its category's schema (2.4)
- pass selection can skip registers for an MCU without waiting for the
  schematic pass to finish
- validation applies the right rules from the start
- a colliding or unusable part number is caught before three passes are paid
  for

One small call added to a run that already costs 200K+ characters.

**Do not add a separate classifier.** Category must keep coming from one
place. Two sources of truth for part type will disagree, and resolving that
is the problem `merge_profiles` already had to solve once for
`component.part_number`.

---

## 3. Pipeline gaps

### 3.1 `batch` does not support passes

`batch` still uses the single-prompt path; `--passes` works only through
`profile`. This blocks running the split at batch rates, which is exactly
where it matters — the large MCUs are the parts that need passes *and* the
parts whose re-extraction costs most.

### 3.2 No `_index.yaml` in the output directory

`ProfileLibrary` (in `pcb_tool_schematic`) already reads an `_index.yaml`
manifest and falls back to globbing when there is none. Generating one means
matching a part number no longer parses every profile. Not urgent — the whole
library is 124 KB and parses in 0.11 s — but it is the lever that keeps
working at 500 parts.

### 3.3 Per-section provenance is only half done

`.meta.json` now records `passes_run` and `passes_skipped` with reasons. It
does not record which extractor version or which chunk hints produced each
*section*. That is what would let a single pass be re-run without redoing the
others.

---

## 4. Extraction quality

### 4.1 Half of the large datasheets is still unlabelled

After the register-page rule, `general` still holds **46% of RP2350 (1.78M
chars)** and **45% of RP2040 (668K)**. Sampling showed real structure in
there — `# Chapter 9. GPIO`, `7.6.1. Power-on reset (POR)`, `3.6.2.5.
Instruction summary`. More vocabulary would recover it. Whether that is worth
doing depends on whether anything downstream wants those sections.

### 4.2 The register pass cannot fit a large register map

It reaches 22% of RP2350's register pages and 49% of RP2040's. Currently
mitigated by skipping the pass entirely for HAL-covered parts, which is the
right call for microcontrollers — but a *peripheral* with a large register map
would hit the same wall and would not be skipped.

Register pages are independent of one another, so the pass could be split into
several sub-calls and the results concatenated. Only worth building if such a
peripheral actually turns up.

### 4.3 Chart-only data is not extracted

Roughly 15% of corpus pages are characteristic curves, and some carry data
that exists nowhere else. The motivating case is on your own board: BSS316N
specifies R_DS(on) only at V_GS = 4.5 V and 10 V, but the kiln controller
drives its gates at 3.3 V — the answer is only in the page-5 curves. A vision
pass would need to target charts specifically and ask for digitised operating
points at stated conditions, not whole curves.

### 4.4 Pinouts that exist only in package drawings

BSS316N's pin numbering appears nowhere but the SOT-23 outline
(`PG-SOT23 / 3 / 1 / 2`). There is no pin table to extract, which is why the
kiln analysis had to assert G/S/D from SOT-23 convention. No current path to
recover this; it may be the same vision problem as 4.3.

### 4.5 The `auto` fallback threshold is unreliable

`auto` falls back to Docling below 150 characters. `9e56b777…` extracts 162
characters of noise — above the threshold, so no fallback, but the content is
worthless. The threshold catches empty PDFs but not near-empty ones.

---

## 5. Known data problems

- **`9e56b777c022540fcce7c7f67825f55e.pdf` has no usable content.** Even with
  Docling OCR it yields 120 characters (`B AA B ROHS`). It is a pure image.
  Replace it with a real datasheet or drop it — step 3 will correctly refuse
  it as-is.
- **`2d53d8d8…pdf` is mislabelled in the README.** The README says it becomes
  `TPS62843.yaml`; it is actually a Shenzhen USB Type-C connector mechanical
  drawing. Fix the example.
- **The plan doc's seed-corpus notes are out of date.** It says 12 profiles
  have no local source PDF; all 15 current profiles now match a source by
  hash. Refresh that Status entry.

---

## 6. Environment and licensing

- **Docling needs `HF_HUB_DISABLE_SYMLINKS=1` on Windows**, or it fails with
  `WinError 1314` creating the HuggingFace model cache. Add to `.env.example`
  and the README troubleshooting section.
- **The Docling compatibility fixes have no test coverage.** Two API breaks
  were fixed by hand (`PyPdfium2DocumentBackend` renamed in docling 2.85;
  `PictureItem.export_to_markdown()` now requires the document). Both were
  silent until exercised, and `auto`'s fallback path depends on them.
- **Licensing is unresolved.** The repo is MIT; `PyMuPDF`, `pymupdf4llm` and
  `pymupdf-layout` are all AGPL-3.0-or-Artifex-commercial, and the last two
  are now required dependencies pulling in ~245 MB. An MIT licence over an
  AGPL-only runtime needs a deliberate decision before publishing.
  `--extractor pymupdf` remains as a dependency-light path.

---

## 7. From the original plan, still not started

- Data repo (`pcb_tool_pdf_output`) publishing flow and the `publish` command
- CI: golden-fixture diffs in this repo, hash re-derivation in the data repo
- `tests/fixtures/` with permissively licensed PDFs and golden outputs
- The component profile schema is still markdown prose; the formal JSON Schema
  is open — and §2 has to land first, since it changes both the category list
  and what each category requires
- Raw-layer output for *schematic* PDFs (text boxes, vectors, coordinates).
  The pipeline targets *datasheet* PDFs; this remains design-only.
