# schematic-extract

Extract structured component profiles from IC datasheet PDFs, for schematic
review and firmware planning. A three-step pipeline turns a datasheet into a
validated YAML profile describing supply rails, pinout, decoupling, design
checks, registers, and init sequences — machine-readable input for schematic
checkers and driver generators.

## Pipeline

```
pdf_input/            source datasheet PDFs
   |  1. extract-text     (PyMuPDF, or Docling OCR for scanned PDFs)
pdf_step1/            raw per-page text JSON + source/content hashes
   |  2. chunk            (heading-aware chunking of datasheet sections)
pdf_step1/chunks/     chunked text JSON
   |  3. profile          (LLM extraction against the profile schema, then validation)
pdf_output/           component profile YAML + meta.json (hashes, versions)
```

Profiles are named after the extracted `component.part_number`, not the input
filename — so `2d53d8d8199c9dd6666d9bc9ac7c3a1e.pdf` becomes `TPS62843.yaml`.
The part number is only known after the LLM has read the datasheet, so this
happens in step 3; the original filename stays in `meta.json` (`source_file`).
When no usable part number is extracted, the source filename is kept rather
than invented — see [Naming and re-runs](#naming-and-re-runs).

By default the three data directories are siblings of this repo
(`../pdf_input`, `../pdf_step1`, `../pdf_output`). Override with the
`PDF_WORKSPACE`, `PDF_INPUT_DIR`, `PDF_STEP1_DIR`, `PDF_OUTPUT_DIR`
environment variables (a `.env` file in the repo root is loaded
automatically — see `.env.example`).

## Install

Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   Linux/macOS: source .venv/bin/activate
pip install -e ".[openai]"        # or [claude], [claude-code], [docling], [all]
```

## First-run setup

```bash
schematic-extract setup           # pick a backend, paste your key (hidden input)
schematic-extract doctor --live   # verify install, key safety, and the LLM connection
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
`echo $OPENAI_API_KEY | schematic-extract setup --backend openai`.

## Usage

```bash
# Everything at once: process every PDF in pdf_input/ that lacks a profile
schematic-extract batch

# Or step by step
schematic-extract extract-text MCP73831.pdf            # -> pdf_step1/MCP73831.json
schematic-extract chunk MCP73831.pdf                   # -> pdf_step1/chunks/MCP73831.json
schematic-extract profile MCP73831.pdf                 # -> pdf_output/MCP73831.yaml + .meta.json

# Re-validate a profile against the schema rules
schematic-extract validate MCP73831.yaml
```

Options:

- `--extractor pymupdf|docling|auto` — `auto` falls back to Docling OCR when
  the PDF has no usable text layer (< 150 chars extracted).
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
recorded in `pdf_output/.batches/<batch_id>.json` before polling begins, so
interrupting the poll is safe:

```bash
schematic-extract batch                              # submit + wait + write
schematic-extract batch --resume msgbatch_01ABC...   # collect a batch later
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
schematic-extract extract-text foo.pdf --source-url https://vendor/foo.pdf
schematic-extract backfill-provenance --dry-run   # preview
schematic-extract backfill-provenance             # fill in older records
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

Configuration (managed by `schematic-extract setup`; env vars override `.env`):

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
[the plan](pdf-schematic-extraction-plan.md#hashing)):

- **source hash** — SHA-256 of the PDF bytes; identity of the record
- **content hash** — SHA-256 of the canonicalized output; detects
  nondeterminism and hand edits

## Data repositories

Bulk extraction outputs will live in a separate data repo
(`schematic-extract-data`) so code clones stay fast and outputs derived from
copyrighted PDFs carry their own license and review workflow.

- Data repo: **TBD — not yet published**
- Design, layout, hashing, and contribution flow:
  [pdf-schematic-extraction-plan.md](pdf-schematic-extraction-plan.md)

Source PDFs are not committed unless their license permits; records store the
source hash + URL instead.

## Project status

Early development. See
[pdf-schematic-extraction-plan.md](pdf-schematic-extraction-plan.md) for the
roadmap and current status. Extraction pipeline originated in a private
KiCad/firmware-review project and is being generalized here.

## License

MIT — see [LICENSE](LICENSE). (The data repo will carry its own license.)
