# pcb_tool_pdf_extract

Extract structured component profiles from IC datasheet PDFs, for schematic
review and firmware planning. A three-step pipeline turns a datasheet into a
validated YAML profile describing supply rails, pinout, decoupling, design
checks, registers, and init sequences — machine-readable input for schematic
checkers and driver generators.

## Pipeline

```
pdf_input/             source datasheet PDFs
   |  1. extract-text      (pymupdf4llm markdown; Docling OCR for scanned PDFs)
pdf_step1/             raw per-page text JSON + source/content hashes
   |  2. chunk             (heading-aware chunking of datasheet sections)
pdf_step1/chunks/      chunked text JSON
   |  3. profile           (LLM extraction against the profile schema, then validation)
pcb_tool_pdf_output/   component profile YAML + meta.json (hashes, versions)
```

Profiles are named after the extracted `component.part_number`, not the input
filename — so `2d53d8d8199c9dd6666d9bc9ac7c3a1e.pdf` becomes `TPS62843.yaml`.
The part number is only known after the LLM has read the datasheet, so this
happens in step 3; the original filename stays in `meta.json` (`source_file`).
When no usable part number is extracted, the source filename is kept rather
than invented — see [Naming and re-runs](#naming-and-re-runs).

By default the three data directories are siblings of this repo
(`../pdf_input`, `../pdf_step1`, `../pcb_tool_pdf_output`). Override with the
`PDF_WORKSPACE`, `PDF_INPUT_DIR`, `PDF_STEP1_DIR`, `PDF_OUTPUT_DIR`
environment variables (a `.env` file in the repo root is loaded
automatically — see `.env.example`).

## Tables

Reading-order text flattens a table into a column of values, losing which
column each value belonged to. Step 1 therefore extracts each page as
**markdown**, via `pymupdf4llm`, with tables rendered in place:

```
|**NO.**|**PIN**<br>**NAME**|**FUNCTION**|**DESCRIPTION**|
|1|AINCOM|Analog input|Common analog input for single-ended measurements|
```

Where it cannot separate two columns it merges them under a joint header and
says so — `**MIN**<br>**TYP**` — so `|-10<br>+-0.1|10|` stays recoverable
rather than becoming a guess.

That honesty is the whole reason for the choice. Assembling tables from
PyMuPDF's `find_tables()` was tried and rejected: datasheet numeric columns are
separated by whitespace rather than rules, so the detector merged MIN/TYP/MAX
into one cell and the renderer then repeated that cell across every column it
spanned. `|±0.5|±0.5|±0.5|nA|` asserts a typical value as the minimum and
maximum too, and nothing else about the row looks wrong. Measured across a
17-datasheet corpus, 34% of detected tables were damaged that way, concentrated
on absolute maximum ratings, recommended operating conditions and electrical
characteristics — the tables profiles are built from.

`tables.py` keeps a narrow watch for that failure returning: a data row whose
MIN/TYP/MAX columns all hold the same value. Only columns whose header
promises a single value are compared, because a broader rule is useless noise —
a resolution of "16 (16)" really is the same at every gain.

**OCR is disabled.** Left alone, `pymupdf4llm` runs OCR over pages with no text
layer whenever any OCR backend is importable, and installing `docling` for the
OCR extractor makes one importable. Step-1 output — and so `content_sha256` —
would then depend on which unrelated optional packages a machine has. Scanned
PDFs are served by the explicit `docling` extractor instead.

Cost: about 0.2 s per page against 0.02 s for plain text, and 4–26% more
characters than raw text. Step 1 is cached, so that is paid once per datasheet.
Use `--extractor pymupdf` for plain text with no table structure and no
dependency beyond PyMuPDF.

## What reaches the model

Step 3 sends the datasheet text to the model. Selection is **additive**: every
chunk is sent unless a character budget forbids it. Heading hints only decide
*what gets dropped first* when a datasheet is too large - they never exclude a
chunk that would otherwise fit.

The budget (`DATASHEET_MAX_CHARS`, default 320,000 characters - roughly 107K
tokens at the pessimistic ratio datasheet tables run at) binds on almost
nothing. Across a 17-datasheet corpus only the 642-page RP2040 exceeds it; the
next largest is 238K characters and is sent whole.

When a datasheet is truncated, step 3 says so and names the sections it
dropped. That matters because a truncated datasheet and one that genuinely
lacks a section produce identical-looking profiles - the moment of truncation
is the only place anyone can still tell the difference.

Headings are matched against a single vocabulary covering the wordings
different vendors use for the same section: ST's "Pin description", TI's "Pin
Configuration and Functions", "Terminal Functions", "Signal Descriptions" and
so on. Matches must look like headings - short, optionally section-numbered
lines - so a paragraph mentioning "the Electrical Characteristics table" does
not mislabel a page.

## Extraction passes

A 1,380-page SoC and a 9-page MOSFET do not fit the same prompt. RP2350 is
3.1M characters; no budget sends it whole, and most of it is per-register
documentation you did not ask for when you wanted a pinout.

`profile --passes` extracts in targeted passes instead, each selecting chunks
by heading hint rather than by budget alone:

| Pass | Sections | Produces |
|---|---|---|
| `schematic` | pin description, absolute maximum, recommended operating, electrical characteristics, power supply, thermal, layout, functional description | `component`, `schematic.*` |
| `software` | interface, register map, register description, timing | `component`, `software.*` |
| `registers` | register page, register map, register description | `software.registers` |

Every pass also gets the document's opening, so each knows which part it is
describing. Results are merged; if two passes disagree on
`component.part_number` the schematic pass wins and the disagreement is
reported rather than silently resolved.

**Passes cost more, not less.** Measured against a single prompt:

| Datasheet | one prompt | three passes | schematic/software coverage |
|---|---|---|---|
| RP2350 (3.1M chars) | 319K | 546K (1.71x) | 10% -> **100%** |
| RP2040 (1.5M chars) | 320K | 495K (1.55x) | 10% -> **100%** |
| STM32L072CZ (271K) | 271K | 814K (3.00x) | 100% -> 100% |
| BSS316N (10K) | 10K | 30K (3.00x) | 100% -> 100% |

For a datasheet that fits, three passes cost exactly three times the input and
gain nothing - each pass is sent the whole document anyway. For one that does
not fit, they cost about 1.6x and take the schematic and software sections
from a tenth of the document to all of it.

### Microcontrollers skip the register pass

Firmware for an MCU is written against the vendor's HAL - STM32Cube, the Pico
SDK - not against the register map, and that map is enormous: RP2350's
register documentation is 1.43M characters, of which one prompt reaches 22%.
Extracting a fifth of a register map nobody would use is worse than not
extracting it, because it looks complete.

So under `--passes auto` the register pass is skipped when the schematic pass
reports a HAL-covered `component.category` (`mcu`, `processor`, `soc`,
`fpga`). Peripheral ICs keep it - their registers are exactly the contract a
driver is written against, which is why the ADS114S06B's 18 registers,
ST7567's 23 and M95P32-I's 4 earn their place. Naming the pass explicitly
(`--passes registers`) always runs it.

`.meta.json` records `passes_run` and `passes_skipped` with the reason, so a
profile with no register map because the part has none is distinguishable from
one that skipped the pass.

That skip also reverses the cost:

| Datasheet | one prompt | three passes | two passes (no registers) |
|---|---|---|---|
| RP2350 | 319K | 546K (1.71x) | **227K (0.71x)** |
| RP2040 | 320K | 495K (1.55x) | **176K (0.55x)** |

For a large microcontroller the split now costs *less* than a single prompt
and still gives the schematic and software passes 100% of their sections.

So use `--passes auto`, which splits only when the datasheet exceeds the
budget. Each pass reports how much of its own material fitted:

```
RP2350 [schematic]: sending 107 of 775 chunks - 10% of the extracted text
    own sections: 105,400/105,400 chars = 100%
RP2350 [registers]: sending 130 of 775 chunks - 10% of the extracted text
    own sections: 316,293/1,430,098 chars = 22%  <- incomplete
```

That last line is the honest limit: RP2350's register documentation is 1.43M
characters and no single prompt holds it. For a part like that, prefer citing
the datasheet from `firmware/references.md` over pre-extracting a register map
nobody reads end to end.

## Install

Python 3.11+.

`pymupdf4llm` is a required dependency. Like PyMuPDF itself it is
**AGPL-3.0 or Artifex commercial**, and it pulls in `pymupdf-layout` and
`onnxruntime` (~245 MB installed). Check that this suits your licensing
position before depending on this package.

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   Linux/macOS: source .venv/bin/activate
pip install -e ".[openai]"        # or [claude], [claude-code], [docling], [all]
```

## First-run setup

```bash
pcb_tool_pdf_extract setup           # pick a backend, paste your key (hidden input)
pcb_tool_pdf_extract doctor --live   # verify install, key safety, and the LLM connection
```

`setup` writes your choices to `.env` in the repo root — the only place a key
is stored. `.env` is gitignored (and `doctor` fails loudly if it ever becomes
tracked or un-ignored), keys are never echoed or printed unmasked, and real
environment variables always take precedence over `.env`, so CI can inject
keys without a file. The `claude-code` backend needs no key at all — it uses
your local Claude Code session.

`doctor` checks Python and dependencies, the pipeline directories, backend
configuration, and key hygiene (including that `.env.example` holds no real
values); `--live` adds one tiny LLM request to prove the key works before you
start a batch run. Piped input works for automation:
`echo $OPENAI_API_KEY | pcb_tool_pdf_extract setup --backend openai`.

## Usage

```bash
# Everything at once: process every PDF in pdf_input/ that lacks a profile
pcb_tool_pdf_extract batch

# Or step by step
pcb_tool_pdf_extract extract-text MCP73831.pdf            # -> pdf_step1/MCP73831.json
pcb_tool_pdf_extract chunk MCP73831.pdf                   # -> pdf_step1/chunks/MCP73831.json
pcb_tool_pdf_extract profile MCP73831.pdf                 # -> pcb_tool_pdf_output/MCP73831.yaml + .meta.json

# Re-validate a profile against the schema rules
pcb_tool_pdf_extract validate MCP73831.yaml
```

Options:

- `--extractor pymupdf4llm|pymupdf|docling|auto` — `pymupdf4llm` (default)
  emits markdown with tables in place; `pymupdf` is plain text with no extra
  dependencies; `auto` falls back to Docling OCR when the PDF has no usable
  text layer (< 150 chars extracted).
- `--backend openai|claude|claude-code` — LLM used in the `profile` step.
  `claude-code` drives a Claude Code session via the Agent SDK and needs no
  API key.
- `--effort low|medium|high|xhigh|max` — how much thinking the model spends
  per datasheet (`claude` backend only; default `high`).
- `--sync` (batch only) — profile one datasheet at a time instead of using the
  Batch API.
- `--resume BATCH_ID` (batch only) — collect a previously submitted batch.

## Batch runs and cost

With the `claude` backend, `batch` submits every datasheet as a single
[Message Batch](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing)
and collects the results, which costs **50% of standard rates**. Steps 1 and 2
still run locally first, so a PDF with no text layer fails before anything is
submitted. Use `--sync` for immediate per-datasheet results at full price; the
`openai` and `claude-code` backends always run sequentially.

Batches usually finish within an hour (24 hours maximum). The submission is
recorded in `pcb_tool_pdf_output/.batches/<batch_id>.json` before polling begins, so
interrupting the poll is safe:

```bash
pcb_tool_pdf_extract batch                              # submit + wait + write
pcb_tool_pdf_extract batch --resume msgbatch_01ABC...   # collect a batch later
```

Two knobs move the bill, in order of return:

- **Batching** halves it, with no effect on output quality.
- **`--effort medium`** cuts the thinking half of the output tokens. Extraction
  against an explicit schema rarely needs `high` — try `medium`, diff the YAML
  against a known-good profile, and keep it if the result holds.

Prompt caching is deliberately not used: the reusable prefix (system prompt +
schema) is only ~5.4K tokens and each datasheet's excerpts are unique, so there
is nothing meaningful to reuse across runs.

## Provenance

`source_sha256` identifies a PDF; without a URL it identifies a file nobody
can find again. Step 1 captures where each PDF came from, with no change to
how you download datasheets: every mainstream browser records the download
location in an NTFS alternate data stream (`Zone.Identifier`) beside the
file, and `extract-text` reads it automatically.

Recorded into step-1 JSON and carried into each profile's `meta.json`:

| Field | Source |
|---|---|
| `source_url` | `Zone.Identifier` (`HostUrl`, falling back to `ReferrerUrl`) |
| `publisher` | host of that URL |
| `retrieved` | file mtime — when the browser saved it |
| `license` | always `proprietary-unverified` unless the sidecar says otherwise |
| `url_source` | `manual` / `sidecar` / `zone-identifier` / `none` |
| `url_note` | set when the URL needs a human look |

Tracking parameters (`gclid`, `utm_*`, `_gl`, `ts`, …) are stripped by
denylist, so meaningful ones — LCSC's `productCode`, Infineon's `folderId` —
survive. Provenance is stored **outside** the hashed payload, so recording it
never changes a `content_sha256`.

**Harvest early.** The `Zone.Identifier` stream does not survive zip
round-trips, FAT/exFAT drives, cloud-sync clients, or git. Run `extract-text`
while the PDFs are still on the volume they were downloaded to.

Precedence is `--source-url` > sidecar > `Zone.Identifier`:

```bash
pcb_tool_pdf_extract extract-text foo.pdf --source-url https://vendor/foo.pdf
pcb_tool_pdf_extract backfill-provenance --dry-run   # preview
pcb_tool_pdf_extract backfill-provenance             # fill in older records
```

`backfill-provenance` matches PDFs to profiles by source hash, so renames on
either side don't matter. Anything it can't resolve is printed as a pasteable
`pdf_input/sources.yaml` block:

```yaml
sources:
  <source_sha256>:
    source_url: "https://vendor.example/datasheet.pdf"
    license: proprietary-unverified
```

The sidecar is keyed by hash rather than filename for the same reason, and is
the place to record a datasheet whose licence genuinely permits
redistribution.

### On `license`

Datasheet licences are effectively never machine-readable and vendor PDFs are
overwhelmingly all-rights-reserved, so every record defaults to
`proprietary-unverified` — the restrictive assumption. The field exists to let
a genuinely redistributable datasheet opt *in* via the sidecar, and to give
the data repo's CI something to gate PDF redistribution on. Nothing infers it.

## Naming and re-runs

Step 3 names its output after `component.part_number`. A part number must be a
single token (`AMS1117`, `LP5907MFX-1.8`); anything containing whitespace is
prose — usually the extractor correctly declining to guess — and the source
filename is kept instead. Two different PDFs claiming the same part number do
not overwrite each other: the second is suffixed with a short source hash and
a warning is printed. Re-profiling the *same* PDF overwrites in place.

Because output names no longer match input names, `batch` decides what to skip
by **source hash** (`meta.json`'s `source_sha256`), not by filename. This also
fixes a case-sensitivity trap: `ads114s06b.pdf` → `ADS114S06B.yaml` looked
processed on Windows and unprocessed on Linux, silently paying for the same
extraction twice in CI. Profiles predating source hashing still fall back to
the old filename check.

Renaming a profile by hand is safe — identity is the source hash, not the
name. Nothing is ever deleted automatically; when a rename leaves an old file
behind, the run says so and leaves it for you to remove.

Configuration (managed by `pcb_tool_pdf_extract setup`; env vars override `.env`):

| Variable | Purpose | Default |
|---|---|---|
| `DATASHEET_BACKEND` | LLM backend | `openai` |
| `DATASHEET_MODEL` | Model name | per backend |
| `DATASHEET_EFFORT` | Thinking effort (`claude` backend only) | `high` |
| `OPENAI_API_KEY` | for `openai` backend | — |
| `ANTHROPIC_API_KEY` | for `claude` backend | — |
| `PDF_WORKSPACE` | parent of the data dirs | repo parent |

## Output schema

The component profile schema (the collaboration contract) is defined in
[schema/component_profile_schema.md](schema/component_profile_schema.md):
categories (sensor, power, mcu, mux, interface, passive), required sections
per category, field reference, and naming conventions. Profiles are validated
against it after LLM extraction; the extractor is instructed to omit rather
than guess.

Every step-1 file and profile carries two hashes (see
[the plan](pdf-pcb_tool_pdf_extraction-plan.md#hashing)):

- **source hash** — SHA-256 of the PDF bytes; identity of the record
- **content hash** — SHA-256 of the canonicalized output; detects
  nondeterminism and hand edits

## Data repositories

Bulk extraction outputs will live in a separate data repo
(`pcb_tool_pdf_extract-data`) so code clones stay fast and outputs derived from
copyrighted PDFs carry their own license and review workflow.

- Data repo: **TBD — not yet published**
- Design, layout, hashing, and contribution flow:
  [pdf-pcb_tool_pdf_extraction-plan.md](pdf-pcb_tool_pdf_extraction-plan.md)

Source PDFs are not committed unless their license permits; records store the
source hash + URL instead.

## Project status

Early development. See
[pdf-pcb_tool_pdf_extraction-plan.md](pdf-pcb_tool_pdf_extraction-plan.md) for the
roadmap and current status. Extraction pipeline originated in a private
KiCad/firmware-review project and is being generalized here.

## License

MIT — see [LICENSE](LICENSE). (The data repo will carry its own license.)
