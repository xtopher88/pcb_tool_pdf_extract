# PDF Schematic Extraction — Repo & Data Plan

*Draft: September 2026 — living document, updated as development proceeds*

## Goal

An open source library that extracts structured data from schematic PDFs for
circuit analysis and firmware planning, with a shared, collaboratively grown
dataset of extraction outputs.

## Status (updated 2026-09-05)

**Done**

- Code repo scaffolded here as `schematic-extract` (src layout, MIT license,
  `pip install -e .` + `schematic-extract` CLI). Pipeline ported and
  generalized from the private `llm_kicad` project.
- Three-step pipeline working: `extract-text` (PyMuPDF, Docling OCR fallback
  via `--extractor auto`) → `chunk` (heading-aware) → `profile` (LLM
  extraction + schema validation). Plus `batch` and `validate` commands.
- Local working directories (distinct from the future data repo layout —
  these are the developer's scratch corpus, not the shared dataset):
  - `../pdf_input/` — source PDFs
  - `../pdf_step1/` — raw extracted text before LLM processing (chunks in
    `pdf_step1/chunks/`)
  - `../pdf_output/` — component profile YAML + `.meta.json`
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
  `pdf_output/.batches/<id>.json` before polling, so an interrupted poll is
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
- Onboarding & key management (`setup_env.py`): `schematic-extract setup`
  (interactive backend picker, hidden key input, writes `.env` only) and
  `schematic-extract doctor [--live]` (deps, dirs, config, key hygiene, and
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
  `pdf_output/` with generated `.meta.json` (llm backend/model recorded as
  unknown, `imported` note set). Known gaps in the seed corpus:
  - 12 profiles have no source PDF locally (AP2280, BMM350, ER-TFT019-1,
    LIS2MDL, LP5907, LSM6DSOX, MCP73831, MLX90393, ST7789P3, STM32U5F7VJ,
    TCA9548A, TPS62843) — meta lacks source hashes until the PDFs are added
  - 11 PDFs have no profile yet (candidates for `batch`)
  - 6 legacy profiles fail schema validation (missing required sections,
    e.g. `schematic.decoupling`, `software.interface`) — regenerate with the
    new pipeline or hand-fill

**Not started**

- Data repo (`schematic-extract-data`) creation and the `publish` CLI command
- CI (golden-fixture diffs in the code repo; hash re-derivation in the data
  repo)
- `tests/fixtures/` with permissively licensed PDFs + golden outputs
- Raw-layer output for *schematic* PDFs (text boxes, vectors, coordinates) —
  current pipeline targets *datasheet* PDFs; the raw schematic layer below
  remains design-only

## Two repositories

### 1. Code repo (`schematic-extract`)

Contains:

- Library, CLI, and documentation
- **Output schema** (JSON Schema) with a `schema_version` field — this is the
  collaboration contract; contributors build tooling against it without
  needing the dataset
- `tests/fixtures/`: a small set of permissively licensed or self-made PDFs
  (own KiCad exports, open-hardware schematics) plus golden expected outputs.
  Small and stable; CI diffs against these.
- A pinned data-repo tag in docs/tests for reproducibility

### 2. Data repo (`schematic-extract-data`)

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

1. `schematic-extract publish <pdf>`: runs extraction, computes both hashes,
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
- Create the `schematic-extract-data` repo and implement `publish`
- Seed `tests/fixtures/` with permissively licensed or self-made PDFs and
  golden outputs; wire up CI
- **Deferred: automated PDF fetching.** The designer downloads datasheets by
  hand during development, and vendor sites (login walls, JS gates, per-vendor
  URL schemes) make a general downloader its own project. Revisit only when
  the data repo needs a `fetch` script to rebuild a corpus from hash + URL, or
  if the server-side `workflow_dispatch` publish path is chosen.
  Prerequisite, worth doing early: record `source_url` (+ `license`, retrieval
  date) as provenance at `extract-text` time so a later fetcher has the data
  it needs — the seed corpus's 12 PDF-less profiles show the cost of not
  capturing it.
