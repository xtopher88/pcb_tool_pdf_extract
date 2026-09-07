from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .chunking import Chunk, chunk_pages, save_chunks
from .config import Settings, ensure_dirs
from . import batch_api
from .extract_profile import (
    PASSES,
    PASS_NAMES,
    get_pass,
    load_system_prompt,
    recommend_passes,
    skip_registers_reason,
    merge_profiles,
    parse_and_validate,
    parse_yaml_response,
    prepare_user_prompt,
    select_chunks,
    select_for_pass,
)
from .hashing import content_sha256, file_sha256
from .llm_client import EFFORT_CHOICES, first_text_block, make_client
from .pdf_extract import extract_pdf_text, save_extracted_text
from .provenance import SIDECAR_NAME, harvest, load_sidecar, sidecar_stub
from .tables import audit
from .validate_profile import validate_profile

_EXTRACTOR_CHOICES = ["pymupdf4llm", "pymupdf", "docling", "auto"]
_BACKEND_CHOICES = ["openai", "claude", "claude-code"]
# Written by step 1, copied into each profile's meta.json.
_PROVENANCE_FIELDS = ("source_url", "publisher", "retrieved",
                      "license", "url_source", "url_note")


def _resolve_pdf(settings: Settings, pdf_name: str) -> Path:
    """Accept either a bare filename in the input dir or a full path."""
    as_path = Path(pdf_name)
    if as_path.is_file():
        return as_path
    candidate = settings.input_dir / pdf_name
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"PDF not found: {pdf_name} (looked in {settings.input_dir})"
    )


def cmd_extract_text(pdf_name: str, extractor: str = "pymupdf4llm",
                     source_url: str | None = None) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    pdf_path = _resolve_pdf(settings, pdf_name)
    out_path = settings.step1_dir / f"{pdf_path.stem}.json"

    data = extract_pdf_text(pdf_path, extractor=extractor,
                            source_url=source_url,
                            sidecar=load_sidecar(settings.input_dir))
    save_extracted_text(data, out_path)

    report = audit(data["pages"])
    print(f"  {data['page_count']} pages, {report['table_rows']} table rows")
    if report["suspect_rows"]:
        # Never seen from pymupdf4llm; a non-zero count means the renderer
        # started repeating a spanned cell across the columns it covers, which
        # would put wrong values into profiles without any other symptom.
        print(f"  WARNING: {len(report['suspect_rows'])} table row(s) repeat one "
              f"value across three or more columns - check "
              f"page(s) {sorted({r['page'] for r in report['suspect_rows']})[:8]}")
    print(f"Wrote {out_path}")


def cmd_chunk(pdf_name: str) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    stem = Path(pdf_name).stem
    raw_path = settings.step1_dir / f"{stem}.json"
    chunks_path = settings.chunks_dir / f"{stem}.json"

    extracted = json.loads(raw_path.read_text(encoding="utf-8"))
    chunks = chunk_pages(extracted)
    save_chunks(chunks, chunks_path)
    print(f"Wrote {chunks_path}")


def _prepare_request(settings: Settings, pdf_name: str,
                     pass_name: str | None = None) -> tuple[str, str, str]:
    """Build the (system_prompt, user_prompt, stem) for one datasheet.

    Reads only local step-1 artefacts - no API call - so a batch run can
    prepare and sanity-check every prompt before spending anything.

    With `pass_name`, chunks are chosen for that pass's sections rather than
    by budget alone; the schema is narrowed to the keys the pass produces so
    the model is not asked for sections it was not given the text for.
    """
    stem = Path(pdf_name).stem
    chunks_path = settings.chunks_dir / f"{stem}.json"

    raw_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in raw_chunks]

    system_prompt = load_system_prompt(settings.prompts_dir / "system.txt")
    schema = settings.schema_path.read_text(encoding="utf-8")

    if pass_name:
        extraction_pass = get_pass(pass_name)
        selection = select_for_pass(chunks, extraction_pass, settings.max_chars)
        _report_selection(f"{stem} [{pass_name}]", selection)
        _report_pass_coverage(chunks, extraction_pass, selection)
        schema = (
            f"{schema}\n\n"
            f"## This extraction pass\n\n"
            f"You are extracting only: {extraction_pass.purpose}.\n"
            f"Emit only these top-level keys: "
            f"{', '.join(extraction_pass.produces)}.\n"
            f"Omit every other section - a later pass covers it. Do not guess at "
            f"content from sections you were not given."
        )
    else:
        selection = select_chunks(chunks, settings.max_chars)
        _report_selection(stem, selection)

    user_prompt = prepare_user_prompt(
        datasheet_file=Path(pdf_name).name,
        selected_chunks=selection.chunks,
        schema_markdown=schema,
    )
    return system_prompt, user_prompt, stem


def _report_pass_coverage(chunks, extraction_pass, selection) -> None:
    """Say how much of the pass's own material actually fitted.

    A pass that got all of its sections and one that got a fifth of them
    produce profiles that look alike, so the difference has to be visible
    here.
    """
    want = sum(len(c.text) for c in chunks if c.heading_hint in extraction_pass.hints)
    if not want:
        print(f"    no chunks matched this pass's sections; sending general content")
        return
    got = sum(len(c.text) for c in selection.chunks
              if c.heading_hint in extraction_pass.hints)
    pct = 100 * got // want
    note = "" if pct == 100 else "  <- incomplete; raise DATASHEET_MAX_CHARS or split further"
    print(f"    own sections: {got:,}/{want:,} chars = {pct}%{note}")


def _report_selection(stem: str, selection) -> None:
    """Say how much of the datasheet is being sent.

    A truncated datasheet and a datasheet that genuinely lacks a section look
    identical in the resulting profile, so the truncation has to be visible
    here - it is the only place anyone can still tell the difference.
    """
    if selection.complete:
        print(f"  {stem}: sending all {len(selection.chunks)} chunks "
              f"({selection.included_chars:,} chars)")
        return
    dropped_sections = sorted({chunk.heading_hint for chunk in selection.dropped})
    print(f"  {stem}: sending {len(selection.chunks)} of "
          f"{len(selection.chunks) + len(selection.dropped)} chunks - "
          f"{selection.coverage:.0%} of the extracted text "
          f"({selection.included_chars:,} of {selection.total_chars:,} chars)")
    print(f"    over the character budget; dropped lowest-priority sections: "
          f"{', '.join(dropped_sections)}")
    print("    raise DATASHEET_MAX_CHARS if the model's context allows it")


# A manufacturer part number is a single token. Anything with whitespace is
# prose - typically the extractor correctly declining to guess ("Not specified
# in provided excerpts") - and must not become a filename. Excluding the path
# separators also keeps a model-supplied string from escaping the output dir.
_PART_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _part_number_stem(profile: dict) -> str | None:
    """The part number to name this profile's files after, or None."""
    component = profile.get("component") if isinstance(profile, dict) else None
    if not isinstance(component, dict):
        return None
    raw = component.get("part_number")
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().strip("\"'")
    return candidate if _PART_NUMBER_RE.match(candidate) else None


def _resolve_out_stem(settings: Settings, desired: str,
                      source_sha256: str) -> str:
    """Guard against two different datasheets claiming the same part number.

    Re-profiling the same PDF overwrites in place; a genuine collision between
    different sources is suffixed rather than silently clobbering the earlier
    profile.
    """
    meta_path = settings.output_dir / f"{desired}.meta.json"
    if not meta_path.is_file():
        return desired
    try:
        existing = json.loads(meta_path.read_text(encoding="utf-8")).get("source_sha256")
    except (OSError, json.JSONDecodeError):
        return desired
    if not existing or existing == source_sha256:
        return desired
    suffixed = f"{desired}__{source_sha256[:8]}"
    print(f"  WARNING: {desired}.yaml already describes a different source PDF "
          f"({existing[:8]}...); writing {suffixed}.yaml instead")
    return suffixed


def _write_profile(settings: Settings, stem: str, response_text: str,
                   backend: str, model: str, effort: str,
                   via_batch: bool = False,
                   passes_run: list[str] | None = None,
                   passes_skipped: dict[str, str] | None = None) -> str:
    """Parse a model response into pcb_tool_pdf_output/<name>.yaml + .meta.json.

    Files are named after `component.part_number` when the model extracted a
    usable one, falling back to the source PDF's stem - which is what keeps
    downloads like `2d53d8d8...pdf` from producing anonymous profiles. The
    original filename stays recoverable from meta.json's `source_file`.

    Returns the stem actually written; raises if the response is not parseable.
    """
    extracted = json.loads(
        (settings.step1_dir / f"{stem}.json").read_text(encoding="utf-8")
    )
    profile, issues = parse_and_validate(response_text)

    source_sha256 = extracted["source_sha256"]
    part_number = _part_number_stem(profile)
    if part_number:
        out_stem = _resolve_out_stem(settings, part_number, source_sha256)
    else:
        out_stem = stem
        print("  no usable component.part_number - keeping the source filename")

    out_path = settings.output_dir / f"{out_stem}.yaml"
    meta_path = settings.output_dir / f"{out_stem}.meta.json"
    out_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    meta = {
        "source_file": extracted["file"],
        "source_sha256": extracted["source_sha256"],
        "step1_content_sha256": extracted["content_sha256"],
        "profile_content_sha256": content_sha256(profile),
        "extractor_version": __version__,
        "pdf_extractor": extracted.get("extractor", "pymupdf"),
        "llm_backend": backend,
        "model": model,
        "effort": effort,
        "batch_api": via_batch,
        "date": date.today().isoformat(),
        # Which passes produced this profile, and why any were left out. A
        # profile with no register map because the part has none and one that
        # skipped the pass are otherwise indistinguishable.
        **({"passes_run": passes_run} if passes_run else {}),
        **({"passes_skipped": passes_skipped} if passes_skipped else {}),
        # Carried through from step 1 - the Zone.Identifier stream it came
        # from may not exist by the time anyone reads this profile.
        **{key: extracted.get(key) for key in _PROVENANCE_FIELDS},
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"Wrote {meta_path}")

    if out_stem != stem:
        print(f"  named from part_number: {stem}.pdf -> {out_stem}.yaml")
        _note_superseded(settings, stem, out_path)

    if issues:
        print("\nValidation issues:")
        for issue in issues:
            print(f"- {issue}")
    return out_stem


def _note_superseded(settings: Settings, stem: str, new_path: Path) -> None:
    """Flag - but never delete - a profile left behind under the old name."""
    old_path = settings.output_dir / f"{stem}.yaml"
    if not old_path.is_file():
        return
    try:
        if os.path.samefile(old_path, new_path):
            return  # case-insensitive filesystem: same file, already rewritten
    except OSError:
        return
    print(f"  note: {old_path.name} is superseded by {new_path.name} "
          "and was left in place - delete it when you are happy with the new one")


def cmd_profile(pdf_name: str, backend: str | None = None,
                effort: str | None = None,
                passes: list[str] | str | None = None) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    resolved_backend = backend or settings.backend
    resolved_effort = effort or settings.effort
    print(f"  backend: {resolved_backend}")
    llm = make_client(resolved_backend, settings.model_name, effort=resolved_effort)

    auto = passes == "auto"
    if auto:
        stem = Path(pdf_name).stem
        chunks = [Chunk(**c) for c in json.loads(
            (settings.chunks_dir / f"{stem}.json").read_text(encoding="utf-8"))]
        passes = recommend_passes(chunks, settings.max_chars)
        print(f"  passes: {', '.join(passes) if passes else 'none - datasheet fits one prompt'}")

    if not passes:
        system_prompt, user_prompt, stem = _prepare_request(settings, pdf_name)
        response = llm.generate_yaml_profile(system_prompt, user_prompt)
        _write_profile(settings, stem, response, resolved_backend,
                       settings.model_name, resolved_effort)
        return

    stem = Path(pdf_name).stem
    by_pass: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    requested = list(passes)

    for pass_name in requested:
        # The schematic pass runs first and establishes the category, so by the
        # time the register pass comes up we know whether a HAL covers it.
        # An explicitly requested pass is always honoured; only `auto` skips.
        if pass_name == "registers" and auto:
            reason = skip_registers_reason(by_pass.get("schematic") or {})
            if reason:
                print(f"  pass: registers - SKIPPED ({reason})")
                skipped[pass_name] = reason
                continue

        print(f"  pass: {pass_name} - {get_pass(pass_name).purpose}")
        system_prompt, user_prompt, stem = _prepare_request(settings, pdf_name, pass_name)
        response = llm.generate_yaml_profile(system_prompt, user_prompt)
        try:
            by_pass[pass_name] = parse_yaml_response(response)
        except ValueError as error:
            # One pass failing must not lose the others - they are separate
            # requests and the rest of the profile is still worth writing.
            print(f"    pass {pass_name} did not return usable YAML: {error}")

    if not by_pass:
        raise ValueError(f"No pass produced a usable profile for {pdf_name}")

    merged, notes = merge_profiles(by_pass)
    for note in notes:
        print(f"  NOTE: {note}")
    _write_profile(settings, stem, yaml.dump(merged, sort_keys=False, allow_unicode=True),
                   resolved_backend, settings.model_name, resolved_effort,
                   passes_run=sorted(by_pass), passes_skipped=skipped)


def cmd_validate(profile_path: str) -> int:
    path = Path(profile_path)
    if not path.is_file():
        settings = Settings.load()
        path = settings.output_dir / profile_path
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = validate_profile(profile)
    for err in result.errors:
        print(f"ERROR: {err}")
    for warn in result.warnings:
        print(f"warning: {warn}")
    print("OK" if result.ok else "FAILED")
    return 0 if result.ok else 1


def _profiled_sources(output_dir: Path) -> dict[str, str]:
    """source_sha256 -> profile stem, read from every meta.json.

    The skip check keys on this rather than on `<pdf stem>.yaml` existing:
    profiles are named after the part number, so the output filename no longer
    matches the input filename. Filename matching also silently depended on
    Windows' case-insensitivity - `ads114s06b.pdf` vs `ADS114S06B.yaml` looked
    processed here and unprocessed on Linux, resubmitting a paid extraction.
    """
    index: dict[str, str] = {}
    for meta_path in output_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        digest = data.get("source_sha256")
        if digest:
            index[digest] = meta_path.name[: -len(".meta.json")]
    return index


def _already_profiled(settings: Settings, pdf_path: Path,
                      index: dict[str, str]) -> str | None:
    """Name of the existing profile for this PDF, or None."""
    existing = index.get(file_sha256(pdf_path))
    if existing:
        return existing
    # Imported profiles predate source hashing; fall back to the old check so
    # this stays at least as conservative as before.
    if (settings.output_dir / f"{pdf_path.stem}.yaml").is_file():
        return pdf_path.stem
    return None


def cmd_backfill_provenance(dry_run: bool = False) -> int:
    """Add source_url / retrieved / license to records written before
    provenance was captured.

    Matches PDFs to profiles by source hash, so it works regardless of how
    either file has since been renamed. Run it while the source PDFs are
    still on the NTFS volume they were downloaded to - the Zone.Identifier
    stream is what makes the backfill possible, and copying the corpus
    through a zip or a USB stick destroys it.
    """
    settings = Settings.load()
    ensure_dirs(settings)
    sidecar = load_sidecar(settings.input_dir)

    # source_sha256 -> harvested provenance, from the PDFs we still have
    by_hash: dict[str, tuple[Path, object]] = {}
    for pdf_path in sorted(settings.input_dir.glob("*.pdf")):
        digest = file_sha256(pdf_path)
        by_hash[digest] = (pdf_path, harvest(pdf_path, digest, sidecar=sidecar))

    updated, already, unresolved, orphaned = [], [], [], []

    for meta_path in sorted(settings.output_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  skipped {meta_path.name}: {exc}")
            continue

        name = meta_path.name[: -len(".meta.json")]
        digest = meta.get("source_sha256")
        if not digest:
            orphaned.append(f"{name} (no source_sha256 - predates hashing)")
            continue
        if meta.get("source_url"):
            already.append(name)
            continue
        entry = by_hash.get(digest)
        if entry is None:
            orphaned.append(f"{name} (source PDF not in {settings.input_dir.name}/)")
            continue

        pdf_path, prov = entry
        if not prov.resolved:
            unresolved.append((digest, pdf_path.name, name))
            continue

        meta.update(prov.as_meta_fields())
        if not dry_run:
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            _backfill_step1(settings, pdf_path.stem, prov)
        updated.append(f"{name}  <-  {prov.source_url}")

    verb = "would update" if dry_run else "updated"
    print(f"\n-- provenance backfill{' (dry run)' if dry_run else ''} --")
    print(f"  {verb}         : {len(updated)}")
    print(f"  already had URL: {len(already)}")
    print(f"  unresolved     : {len(unresolved)}")
    print(f"  not matchable  : {len(orphaned)}")
    for line in updated:
        print(f"    + {line}")
    for line in orphaned:
        print(f"    ? {line}")

    if unresolved:
        print("\nNo URL recoverable for these - paste into "
              f"{settings.input_dir / SIDECAR_NAME} and re-run:\n")
        print(sidecar_stub([(d, f) for d, f, _ in unresolved]))
    return 0


def _backfill_step1(settings: Settings, stem: str, prov) -> None:
    """Keep the step-1 record in step with the profile. Provenance lives
    outside the hashed payload, so this leaves content_sha256 untouched."""
    path = settings.step1_dir / f"{stem}.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    before = data.get("content_sha256")
    data.update(prov.as_meta_fields())
    assert data.get("content_sha256") == before, "provenance must not alter the content hash"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _summary(processed: list[str], skipped: list[str], failed: list[str]) -> None:
    print("\n-- Summary --")
    print(f"  Processed : {len(processed)}")
    print(f"  Skipped   : {len(skipped)}")
    print(f"  Failed    : {len(failed)}")
    if skipped:
        print("  (use --force to reprocess skipped)")
    for name in failed:
        print(f"  FAILED: {name}")


def _collect_batch(settings: Settings, llm, batch_id: str, backend: str,
                   effort: str) -> tuple[list[str], list[str]]:
    """Write every finished result of a batch. Returns (processed, failed)."""
    state = batch_api.load_state(settings.output_dir, batch_id)
    mapping = state["mapping"]
    model = state.get("model", settings.model_name)

    print(f"\nWaiting on batch {batch_id} (safe to Ctrl-C; resume with "
          f"'pcb_tool_pdf_extract batch --resume {batch_id}')")
    batch = batch_api.poll_until_done(llm, batch_id)
    print(f"  batch ended: succeeded={batch.request_counts.succeeded} "
          f"errored={batch.request_counts.errored}")

    processed, failed = [], []
    in_tokens = out_tokens = 0

    for result in llm.client.messages.batches.results(batch_id):
        stem = mapping.get(result.custom_id, result.custom_id)
        kind = result.result.type
        if kind != "succeeded":
            detail = kind
            if kind == "errored":
                detail = f"errored ({result.result.error.type})"
            print(f"\n-- {stem} --\n  ERROR: {detail}")
            failed.append(stem)
            continue

        message = result.result.message
        in_tokens += message.usage.input_tokens
        out_tokens += message.usage.output_tokens
        print(f"\n-- {stem} --")
        try:
            text = first_text_block(message)
            processed.append(_write_profile(
                settings, stem, text, backend, model, effort, via_batch=True))
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed.append(stem)

    state["collected_at"] = datetime.now(timezone.utc).isoformat()
    batch_api.state_path(settings.output_dir, batch_id).write_text(
        json.dumps(state, indent=2), encoding="utf-8")

    print(f"\n  tokens: {in_tokens:,} in / {out_tokens:,} out "
          "(output includes thinking)")
    cost = batch_api.estimate_cost(model, in_tokens, out_tokens)
    if cost is not None:
        print(f"  estimated batch-rate cost: ${cost:.2f} "
              f"(${cost * 2:.2f} at standard rates)")
    return processed, failed


def cmd_batch(force: bool = False, backend: str | None = None,
              extractor: str = "pymupdf", effort: str | None = None,
              sync: bool = False, resume: str | None = None) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    resolved_backend = backend or settings.backend
    resolved_effort = effort or settings.effort

    if resume:
        if resolved_backend != "claude":
            raise SystemExit("--resume applies to the 'claude' backend only.")
        llm = make_client(resolved_backend, settings.model_name, effort=resolved_effort)
        try:
            processed, failed = _collect_batch(
                settings, llm, resume, resolved_backend, resolved_effort)
        except FileNotFoundError as exc:
            raise SystemExit(str(exc))
        _summary(processed, [], failed)
        return

    pdfs = sorted(settings.input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {settings.input_dir}")
        return

    skipped, processed, failed = [], [], []
    prepared: list[batch_api.BatchItem] = []

    # Steps 1-2 are local and cost nothing, so they run for every datasheet
    # up front - a PDF with no text layer fails here rather than after a
    # batch has already been paid for.
    profiled = _profiled_sources(settings.output_dir)

    for pdf_path in pdfs:
        existing = None if force else _already_profiled(settings, pdf_path, profiled)
        if existing:
            label = pdf_path.name
            if existing != pdf_path.stem:
                label = f"{pdf_path.name} (profiled as {existing}.yaml)"
            skipped.append(label)
            continue

        print(f"\n-- {pdf_path.name} --")
        try:
            print("  [1/3] extract-text")
            cmd_extract_text(pdf_path.name, extractor=extractor)
            print("  [2/3] chunk")
            cmd_chunk(pdf_path.name)
            system_prompt, user_prompt, stem = _prepare_request(
                settings, pdf_path.name)
            prepared.append(batch_api.BatchItem(stem, system_prompt, user_prompt))
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed.append(pdf_path.name)

    if not prepared:
        _summary(processed, skipped, failed)
        return

    use_batch_api = resolved_backend == "claude" and not sync
    if resolved_backend != "claude" and not sync:
        print(f"\nNote: the Batch API is Anthropic-only; backend "
              f"'{resolved_backend}' runs sequentially.")

    llm = make_client(resolved_backend, settings.model_name, effort=resolved_effort)

    if use_batch_api:
        print(f"\n  [3/3] profile: submitting {len(prepared)} datasheets as one "
              f"batch ({settings.model_name}, effort={resolved_effort}, "
              "50% of standard rates)")
        batch_id, mapping = batch_api.submit(llm, prepared)
        state_file = batch_api.save_state(
            settings.output_dir, batch_id, mapping,
            settings.model_name, resolved_effort)
        print(f"  batch id: {batch_id}\n  recorded: {state_file}")
        done, batch_failed = _collect_batch(
            settings, llm, batch_id, resolved_backend, resolved_effort)
        processed.extend(done)
        failed.extend(batch_failed)
    else:
        for item in prepared:
            print(f"\n-- {item.stem} --\n  [3/3] profile")
            try:
                response = llm.generate_yaml_profile(
                    item.system_prompt, item.user_prompt)
                processed.append(_write_profile(
                    settings, item.stem, response, resolved_backend,
                    settings.model_name, resolved_effort))
            except Exception as exc:
                print(f"  ERROR: {exc}")
                failed.append(item.stem)

    _summary(processed, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pcb_tool_pdf_extract",
        description="Extract structured component profiles from datasheet PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("extract-text", help="Step 1: PDF -> raw per-page text (pdf_step1/)")
    p1.add_argument("pdf_name")
    p1.add_argument("--extractor", choices=_EXTRACTOR_CHOICES, default="pymupdf4llm",
                    help="PDF extraction backend (default: pymupdf4llm, which "
                         "renders tables as markdown)")
    p1.add_argument("--source-url",
                    help="Where this PDF came from (overrides Zone.Identifier "
                         f"and {SIDECAR_NAME})")

    p2 = sub.add_parser("chunk", help="Step 2: raw text -> heading-aware chunks (pdf_step1/chunks/)")
    p2.add_argument("pdf_name")

    p3 = sub.add_parser("profile", help="Step 3: chunks -> component profile YAML (pcb_tool_pdf_output/)")
    p3.add_argument("pdf_name")
    p3.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="LLM backend (overrides DATASHEET_BACKEND env var)")
    p3.add_argument("--passes", nargs="*", choices=(*PASS_NAMES, "auto"), default=None,
                    metavar="PASS",
                    help="Extract in targeted passes instead of one prompt "
                         f"(choices: {', '.join(PASS_NAMES)}, auto; no value = auto). "
                         "'auto' splits only when the datasheet is too large to "
                         "send whole, which is the only case where it pays.")
    p3.add_argument("--effort", choices=EFFORT_CHOICES,
                    help="Thinking effort, claude backend only "
                         "(overrides DATASHEET_EFFORT; default high)")

    p4 = sub.add_parser("batch", help="Run all three steps for every PDF in pdf_input/ lacking a profile")
    p4.add_argument("--force", action="store_true", help="Reprocess even if a profile already exists")
    p4.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="LLM backend (overrides DATASHEET_BACKEND env var)")
    p4.add_argument("--extractor", choices=_EXTRACTOR_CHOICES, default="pymupdf4llm",
                    help="PDF extraction backend (default: pymupdf4llm, which "
                         "renders tables as markdown)")
    p4.add_argument("--effort", choices=EFFORT_CHOICES,
                    help="Thinking effort, claude backend only "
                         "(overrides DATASHEET_EFFORT; default high)")
    p4.add_argument("--sync", action="store_true",
                    help="Profile one datasheet at a time instead of using the "
                         "Batch API (immediate results, full price)")
    p4.add_argument("--resume", metavar="BATCH_ID",
                    help="Collect results from a previously submitted batch "
                         "instead of submitting a new one")

    p5 = sub.add_parser("validate", help="Validate an existing profile YAML against the schema rules")
    p5.add_argument("profile_path")

    p6 = sub.add_parser("setup", help="First-run setup: choose LLM backend and store API key in .env (gitignored)")
    p6.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="Skip the backend prompt")
    p6.add_argument("--model", help="Override the backend's default model")

    p8 = sub.add_parser("backfill-provenance",
                        help="Add source_url/retrieved/license to records written "
                             "before provenance was captured")
    p8.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing")

    p7 = sub.add_parser("doctor", help="Check installation, directories, config, and key safety")
    p7.add_argument("--live", action="store_true",
                    help="Also make one tiny LLM request to verify the key works")

    args = parser.parse_args()

    if args.cmd == "extract-text":
        cmd_extract_text(args.pdf_name, extractor=args.extractor,
                         source_url=args.source_url)
    elif args.cmd == "chunk":
        cmd_chunk(args.pdf_name)
    elif args.cmd == "profile":
        chosen = args.passes
        if chosen is not None and (not chosen or chosen == ["auto"]):
            chosen = "auto"
        cmd_profile(args.pdf_name, backend=args.backend, effort=args.effort,
                    passes=chosen)
    elif args.cmd == "batch":
        cmd_batch(force=args.force, backend=args.backend,
                  extractor=args.extractor, effort=args.effort,
                  sync=args.sync, resume=args.resume)
    elif args.cmd == "validate":
        raise SystemExit(cmd_validate(args.profile_path))
    elif args.cmd == "setup":
        from .setup_env import cmd_setup
        cmd_setup(backend=args.backend, model=args.model)
    elif args.cmd == "backfill-provenance":
        raise SystemExit(cmd_backfill_provenance(dry_run=args.dry_run))
    elif args.cmd == "doctor":
        from .setup_env import cmd_doctor
        raise SystemExit(cmd_doctor(live=args.live))


if __name__ == "__main__":
    main()
