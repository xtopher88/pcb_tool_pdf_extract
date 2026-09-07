# PDF Schematic Extraction — Repo & Data Plan

*Draft: September 2026 — living document, updated as development proceeds*

## Goal

An open source library that extracts structured data from schematic PDFs for
circuit analysis and firmware planning, with a shared, collaboratively grown
dataset of extraction outputs.

## Status (updated 2026-09-07)

**Done**

- Register-page heading rule and per-section extraction passes (2026-09-07).

  **Heading rule.** Large SoC datasheets are mostly one-page-per-register
  documentation headed `PERIPHERAL: NAME Register`, which matched nothing and
  landed in `general` - where it was both invisible to selection and large
  enough to crowd out everything else. Adding the rule as its own tier-1 hint
  (`register page`, separate from `register description`) dropped `general`
  from **87% to 46%** on RP2350 and **82% to 45%** on RP2040, correctly
  labelling 1.43M and 634K characters. Two bugs surfaced doing it: markdown
  stripping was eating underscores (`IO_BANK0` -> `IO BANK0`, defeating the
  rule), and prose could be read as a heading - now markdown `#` lines are
  authoritative and prose is consulted only on pages that have none.

  **Passes.** `profile --passes` extracts per section (schematic / software /
  registers), selecting by heading hint, each pass also receiving the
  document's opening for identity. Results merge, with the schematic pass
  authoritative on `component.part_number` and disagreements reported.

  **Correction to the earlier plan:** passes were expected to lower total
  input tokens. Measured, they do not. A first implementation topped every
  pass up to the budget, costing 3x a single prompt while each pass still
  dragged in ~200K characters of another pass's material; topping up only when
  the whole document fits brought large datasheets to **1.55-1.71x**. For a
  datasheet that already fits, passes cost a flat **3x and gain nothing**. So
  `--passes auto` splits only above the budget, which is the only case that
  pays. The gain where it does pay is real: RP2350's schematic and software
  passes go from 10% of the document to **100% of their own sections**.

  **Register pass skipped for HAL-covered parts.** Firmware for an MCU is
  written against STM32Cube or the Pico SDK, not the register map, and one
  prompt reaches only 22% of RP2350's 1.43M characters of register
  documentation - a fifth of a map nobody would use, presented as if complete.
  Under `auto` the register pass is skipped when the schematic pass reports
  `component.category` in {mcu, processor, soc, fpga}; peripherals keep it,
  since their registers are the driver contract (ADS114S06B 18, ST7567 23,
  M95P32-I 4, W25Q16JV 3). Verified against the profile corpus: only RP2040
  and STM32L072CZ skip, and both already carry zero registers, so nothing is
  lost. `.meta.json` records `passes_run` / `passes_skipped` with the reason.

  This also reverses the cost finding above: two passes for RP2350 total 227K
  characters against 319K for a single prompt (**0.71x**), and RP2040 176K
  (**0.55x**) - cheaper than one prompt *and* 100% coverage of the schematic
  and software sections.

  `batch` still uses the single-prompt path; wiring passes into it is open.

- Step 1 switched to `pymupdf4llm` markdown (2026-09-06, version 0.3.0),
  replacing the 0.2.0 `find_tables()` trust gate entirely.

  `pymupdf-layout` was evaluated first, since MuPDF suggests it on every run.
  On its own it is **not** worth it: with it active, `find_tables()` returned
  the same collapsed `'-10 +-0.1 10'` cell and the surrounding text got worse
  ("Absoluteinputcurrent"), and `page.get_layout()` returns None. PyMuPDF
  1.28.2 without it produces the identical collapsed cell, so the version bump
  alone changes nothing.

  Chasing its API led to `pymupdf4llm`, which is the real improvement: tables
  render with values in their own columns, and columns it cannot separate are
  merged under a joint header (`**MIN**<br>**TYP**`) rather than guessed at.
  Scanned for the fabrication pattern across the corpus's spec pages: zero
  occurrences. Content markers survive on every datasheet; output is 4-26%
  larger than raw text, against 26-164% for the 0.2.0 append approach, because
  tables are integrated rather than duplicated. STM32L072CZ went from 312K
  chars (0.2.0) to 271K, back clear of the 320K budget.

  Notable win: on M95P32-I it surfaces "Table 1. Signal names" - pad number to
  signal name, in full - which is exactly the mapping that had to be
  hand-verified into the kiln project's `pinmap.yaml`. That override should
  become unnecessary once the profile is regenerated.

  Two decisions worth recording:
  - **OCR disabled** (`use_ocr=OCRMode.NEVER`). Left alone, pymupdf4llm OCRs
    text-less pages whenever any OCR backend is importable, and installing
    `docling` makes one importable - so the same PDF would hash differently on
    two machines. Measured effect on ADS114S06B: 2,461 characters and 9
    seconds. Scanned PDFs remain the `docling` extractor's job.
  - **The replication watchdog was narrowed.** A first version flagged any
    value repeated across three adjacent columns and produced 28 hits on
    ADS114S06B, every one legitimate. It now compares only MIN/TYP/MAX
    columns: silent across 1,162 real table rows, still catches the
    fabricated row.

  `pymupdf4llm` is now a required dependency - AGPL-3.0 or Artifex commercial,
  pulling in `pymupdf-layout` and `onnxruntime`. **Step-1 content hashes change
  again**; re-extraction is required. `--extractor pymupdf` keeps the plain
  text path for anyone who cannot take the dependency.

- Structured tables in step 1 (2026-09-06, `tables.py`, version 0.2.0).
  Investigated whether `find_tables()` should replace reading-order text on
  table pages. **It should not, unconditionally** - measured over the corpus,
  701 tables detected and **34% damaged**, because datasheet numeric columns
  are separated by whitespace rather than rules: the detector merges MIN/TYP/
  MAX into one cell (`extract()` returns `'-10 +-0.1 10'`), and `to_markdown()`
  then repeats that cell across every column it spans. The damage falls
  precisely on absolute maximum ratings, recommended operating conditions and
  electrical characteristics - the tables profiles are built from - and it
  arrives looking like clean structure, which is worse than flat text.

  Implemented instead as a trust gate: a table is emitted only if no
  single-value column (`MIN`/`TYP`/`MAX`/`UNIT` header) holds multiple numbers,
  no value repeats across three adjacent columns, and the shape is non-trivial.
  Accepted tables are **appended to** the page text, never substituted, so a
  gate error cannot lose data. Rejections are recorded per page as
  `tables_rejected` with a reason. Verified on ADS114S06B: the p4 Pin Functions
  table is accepted, the p7-p9 electrical characteristics tables rejected, and
  a page holding both keeps the good one. Cost ~0.1 s/page in step 1 (cached)
  and ~33% more prompt characters; `DATASHEET_TABLES=0` disables it.

  **Step-1 content hashes change** - the extracted content genuinely changed -
  so `extractor_version` is 0.2.0 and every datasheet needs re-extraction.
  Budget interaction: with tables added, stm32l072cz reaches 312K of the 320K
  `DATASHEET_MAX_CHARS` budget, so the largest datasheets are now near the
  truncation point.

- Text-loss fix (2026-09-06). An audit of the 17-datasheet corpus found that
  only **28% of extracted pages ever reached the model** - 356 of 1260. Three
  causes, all now fixed:
  1. `select_relevant_chunks` kept only chunks whose heading was on an
     allowlist, discarding 179 of ~340 chunks tagged `general`. Selection is
     now additive (`select_chunks`): everything is sent subject to a character
     budget, and hints only order what gets dropped when a datasheet is too
     large. Truncation is reported with the sections it dropped.
  2. `IMPORTANT_HEADINGS` and the selection `priority` set had to agree and did
     not - `functional description`, `application information` and
     `timing characteristics` were detected as important and then thrown away.
     There is now one vocabulary with per-section tiers.
  3. The vocabulary was ST-shaped. TI writes "Pin Configuration and Functions",
     which matched nothing - the direct cause of ADS114S06B having no `pinout`
     at all. Vocabulary broadened across vendors; headings are now matched on
     whole pages rather than the first 20 lines, with a heading-shape check so
     prose mentioning a section name does not mislabel a page.

  Result: **28% -> 59% of pages overall, and 16 of 17 datasheets now go through
  whole** (99.8% of pages once the 642-page RP2040 outlier is set aside; the
  one 0% file is an image-only PDF with no text layer). Verified that the
  numbered M95P32 SO8 pin table and TI's "Pin Configuration and Functions"
  section both now reach the prompt. `tests/` added covering all three fixes.
  Profiles have **not** been regenerated yet - the affected ones (ADS114S06B,
  M95P32-I, ST1L05) still carry the old extractions.

- Renamed to match the GitHub repository (2026-09-06): distribution, package
  and CLI are all now `pcb_tool_pdf_extract`, replacing `schematic-extract` /
  `schematic_extract`. One name per repo, so a clone and its imports agree.
  The default output directory follows the data repo to
  `<workspace>/pcb_tool_pdf_output`; `pdf_input` and `pdf_step1` keep their
  names, being local scratch directories rather than repositories. The stale
  `llm_kicad.code-workspace` is now `pcb_tool_pdf_extract.code-workspace` and
  points at the current sibling directories.

- Code repo scaffolded here as `pcb_tool_pdf_extract` (src layout, MIT license,
  `pip install -e .` + `pcb_tool_pdf_extract` CLI). Pipeline ported and
  generalized from the private `llm_kicad` project.
- Three-step pipeline working: `extract-text` (PyMuPDF, Docling OCR fallback
  via `--extractor auto`) → `chunk` (heading-aware) → `profile` (LLM
  extraction + schema validation). Plus `batch` and `validate` commands.
- Local working directories (distinct from the future data repo layout —
  these are the developer's scratch corpus, not the shared dataset):
  - `../pdf_input/` — source PDFs
  - `../pdf_step1/` — raw extracted text before LLM processing (chunks in
    `pdf_step1/chunks/`)
  - `../pcb_tool_pdf_output/` — component profile YAML + `.meta.json`
  - Overridable via `PDF_WORKSPACE` / `PDF_INPUT_DIR` / `PDF_STEP1_DIR` /
    `PDF_OUTPUT_DIR`; defaults are siblings of the repo.
- Both hashes implemented (`hashing.py`): source hash of PDF bytes; content
  hash of canonical JSON (sorted keys, compact separators, timestamps and
  tool versions excluded from the hashed payload). Step-1 JSON and profile
  `.meta.json` both carry them.
- Component profile schema v1.0 committed at
  `schema/component_profile_schema.md` (currently markdown prose — the
  formal JSON Schema is still an open item).
- Smoke-tested on W25Q16JV: extract-text (77 pages) → chunk → validate all
  pass; outputs land in the directories above.
- Batch API for step 3 (`batch_api.py`): with the `claude` backend, `batch`
  submits every prepared datasheet as one Message Batch at 50% of standard
  rates, then collects results. Steps 1-2 run locally first, so an image-only
  PDF fails before anything is submitted. Submissions are recorded to
  `pcb_tool_pdf_output/.batches/<id>.json` before polling, so an interrupted poll is
  resumable (`batch --resume <id>`); `--sync` keeps the sequential path, which
  the non-Anthropic backends always take.
- Cost knobs: `DATASHEET_EFFORT` / `--effort` (claude backend only, default
  `high`); `max_tokens` raised 16000 -> 64000 after measuring that a produced
  profile (ADS114S06B, ~15.6K output tokens) could exceed the old ceiling once
  adaptive thinking shared the same budget. Prompt caching evaluated and
  rejected: the reusable prefix is only ~5.4K tokens.
- Profile naming resolved: step 3 names outputs after `component.part_number`
  (single-token values only; prose like "Not specified in provided excerpts"
  falls back to the source stem), so opaque downloads such as
  `2d53d8d8...pdf` no longer produce anonymous profiles. Colliding part
  numbers from different sources are suffixed with a short source hash rather
  than overwritten. `batch` now decides what to skip by `source_sha256`
  instead of filename - the old check silently depended on Windows'
  case-insensitivity (`ads114s06b.pdf` vs `ADS114S06B.yaml`) and would have
  re-paid for those extractions on Linux/CI.
- Provenance capture (`provenance.py`): `extract-text` harvests `source_url`,
  `publisher` and `retrieved` from the NTFS `Zone.Identifier` stream the
  browser writes at download time - no change to the manual download workflow,
  and it works retroactively (15/17 of the seed corpus had a usable direct PDF
  URL already recorded). Tracking parameters stripped by denylist so
  meaningful ones survive. Precedence `--source-url` > `pdf_input/sources.yaml`
  sidecar (keyed by source hash) > Zone.Identifier; `backfill-provenance
  [--dry-run]` fills older records and prints a pasteable sidecar stub for
  whatever it cannot resolve. Provenance is stored outside the hashed payload,
  verified not to change any existing `content_sha256`. `license` is always
  `proprietary-unverified` unless the sidecar overrides it - the restrictive
  default, since datasheet licences are not machine-readable.
  **Caveat: harvest early** - the stream does not survive zip, FAT/exFAT,
  cloud sync, or git, which is why this runs at step 1 rather than on demand.
- Onboarding & key management (`setup_env.py`): `pcb_tool_pdf_extract setup`
  (interactive backend picker, hidden key input, writes `.env` only) and
  `pcb_tool_pdf_extract doctor [--live]` (deps, dirs, config, key hygiene, and
  an optional one-request LLM connectivity test). Safety model: `.env` is the
  single key store and gitignored (`.env.*` variants too, `.env.example`
  excepted); doctor fails hard if `.env` is tracked/un-ignored or if
  `.env.example` contains a real-looking key; keys are only ever printed
  masked; ambient env vars override `.env` for CI. Tested: piped key entry,
  masking, and the tracked-`.env` failure path.

- Seed corpus migrated from `llm_kicad` (2026-09-03): 17 PDFs into
  `pdf_input/`, 8 raw-text extractions upgraded to the step-1 format (legacy
  `sha256` key renamed to `source_sha256`, content hashes computed,
  provenance marked `extractor: unknown` / `0.0.0-llm_kicad` since the old
  cache didn't record the backend), 8 chunk files, and 18 profiles into
  `pcb_tool_pdf_output/` with generated `.meta.json` (llm backend/model recorded as
  unknown, `imported` note set). Known gaps in the seed corpus:
  - 12 profiles have no source PDF locally (AP2280, BMM350, ER-TFT019-1,
    LIS2MDL, LP5907, LSM6DSOX, MCP73831, MLX90393, ST7789P3, STM32U5F7VJ,
    TCA9548A, TPS62843) — meta lacks source hashes until the PDFs are added
  - 11 PDFs have no profile yet (candidates for `batch`)
  - 6 legacy profiles fail schema validation (missing required sections,
    e.g. `schematic.decoupling`, `software.interface`) — regenerate with the
    new pipeline or hand-fill

**Not started**

- Data repo (`pcb_tool_pdf_extract-data`) creation and the `publish` CLI command
- CI (golden-fixture diffs in the code repo; hash re-derivation in the data
  repo)
- `tests/fixtures/` with permissively licensed PDFs + golden outputs
- Raw-layer output for *schematic* PDFs (text boxes, vectors, coordinates) —
  current pipeline targets *datasheet* PDFs; the raw schematic layer below
  remains design-only

## Two repositories

### 1. Code repo (`pcb_tool_pdf_extract`)

Contains:

- Library, CLI, and documentation
- **Output schema** (JSON Schema) with a `schema_version` field — this is the
  collaboration contract; contributors build tooling against it without
  needing the dataset
- `tests/fixtures/`: a small set of permissively licensed or self-made PDFs
  (own KiCad exports, open-hardware schematics) plus golden expected outputs.
  Small and stable; CI diffs against these.
- A pinned data-repo tag in docs/tests for reproducibility

### 2. Data repo (`pcb_tool_pdf_extract-data`)

Holds bulk extraction outputs. Separate from the code repo because outputs
are large, regenerable, and often derived from copyrighted material.

Alternatives considered:

| Option | When it fits |
|---|---|
| Git LFS in the main repo | Tens of MB total; simplest, but LFS quotas and fork friction |
| Separate data repo | **Default choice** — fast code clones, own license and workflow |
| Hugging Face Datasets / Zenodo | If the corpus should be citable or GB-scale |
| GitHub Releases | Snapshots only; poor for incremental collaboration |

## Raw vs interpreted outputs

The canonical shared artifact is the **raw extraction layer** (text boxes,
vectors, coordinates). Interpretation (components, nets, pin functions) is a
downstream, separately versioned pass. This decouples PDF-parsing improvements
from analysis improvements and keeps the raw layer reusable for later tools
(e.g., an LLM schematic checker).

## Hashing

Two hashes per extraction:

- **Source hash** — SHA-256 of the input PDF bytes. Identity of the record.
  Detects duplicates across contributors.
- **Content hash** — SHA-256 of the canonicalized output (sorted keys, fixed
  float precision, no embedded timestamps). Same PDF + same extractor version
  should yield the same content hash; a mismatch indicates nondeterminism or
  a hand edit.

## Data repo layout

```
data/
  ab/abcd1234…/                 # source hash, sharded by first 2 chars
    source.json                 # url, filename, license, sha256, first_seen
    v0.3.1/
      output.json
      meta.json                 # extractor version, content hash, contributor, date
      corrections.json          # optional, human corrections — never edit output.json
    v0.4.0/
      output.json
      meta.json
manifest.jsonl                  # one line per (source, version); generated in CI
```

Rules:

- Existing version directories are never modified; new extractor versions get
  new subdirectories.
- Source PDFs are not committed unless the license permits. Store hash + URL
  and provide a `fetch` script.
- Prefer line-oriented JSON or per-page/per-sheet files so PR diffs are
  reviewable.
- Human corrections go in `corrections.json` with their own hash, keeping
  machine output and ground truth separate for benchmarking.

## New vs modified detection

| Source hash | Extractor version | Content hash | Result |
|---|---|---|---|
| absent | — | — | **New PDF** — create directory |
| present | absent | — | **New extraction** — add version subdirectory |
| present | present | matches | **No-op** — skip |
| present | present | differs | **Modified** — flag in PR (hand edit or nondeterminism) |

## Contribution flow

Contributors do not push directly to the data repo.

1. `pcb_tool_pdf_extract publish <pdf>`: runs extraction, computes both hashes,
   fetches the manifest from the data repo's `main`, classifies the result
   per the table above, and writes only new/changed files to a fork or branch.
2. The CLI opens a PR via the GitHub API (`gh pr create` or a token helper).
3. Data repo CI:
   - Re-derives hashes from PR contents; rejects mismatches between
     `meta.json` and `output.json`
   - Rejects any change to an existing version directory
   - Requires a `license` field in `source.json`
   - Refuses committed PDFs unless the license allows
4. Merge: human review, or auto-merge when CI passes and the PR is a pure
   addition. Lenient on "new", strict on "modified".

Lower-friction alternative: a `workflow_dispatch` GitHub Action that accepts a
PDF URL, runs extraction server-side with a known extractor build, and opens
the PR itself.

## Dataset versioning

- Tag the data repo on a cadence (e.g., `data-2026.09`); the code repo pins a
  tag in docs and tests.
- `manifest.jsonl` is derived data generated in CI, not hand-edited, to avoid
  merge conflicts.

## Open items

- Finalize the output schema for the raw *schematic* layer (the datasheet
  profile schema v1.0 exists; a formal JSON Schema with `schema_version` is
  still needed for both)
- ~~Canonicalization rules for content hashing~~ — resolved: canonical JSON
  is `sort_keys=True`, compact separators, UTF-8, timestamps/tool versions
  excluded from the hashed payload (`hashing.py`)
- Decide on license for the data repo separate from the code repo (code repo
  is MIT)
- Choose between PR-based CLI publish and server-side Action as the primary
  contribution path
- Create the `pcb_tool_pdf_extract-data` repo and implement `publish`
- Seed `tests/fixtures/` with permissively licensed or self-made PDFs and
  golden outputs; wire up CI
- **Deferred: automated PDF fetching.** The designer downloads datasheets by
  hand during development, and vendor sites (login walls, JS gates, per-vendor
  URL schemes) make a general downloader its own project. Revisit only when
  the data repo needs a `fetch` script to rebuild a corpus from hash + URL, or
  if the server-side `workflow_dispatch` publish path is chosen.
  ~~Prerequisite, worth doing early: record `source_url` (+ `license`,
  retrieval date) as provenance at `extract-text` time~~ — done 2026-09-06
  via `provenance.py` (Zone.Identifier harvest + `sources.yaml` sidecar), so a
  later fetcher now has hash + URL + retrieval date to work from.
