from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .chunking import Chunk, chunk_pages, save_chunks
from .config import Settings, ensure_dirs
from . import batch_api
from .extract_profile import (
    load_system_prompt,
    parse_and_validate,
    prepare_user_prompt,
    select_relevant_chunks,
)
from .hashing import content_sha256
from .llm_client import EFFORT_CHOICES, first_text_block, make_client
from .pdf_extract import extract_pdf_text, save_extracted_text
from .validate_profile import validate_profile

_EXTRACTOR_CHOICES = ["pymupdf", "docling", "auto"]
_BACKEND_CHOICES = ["openai", "claude", "claude-code"]


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


def cmd_extract_text(pdf_name: str, extractor: str = "pymupdf") -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    pdf_path = _resolve_pdf(settings, pdf_name)
    out_path = settings.step1_dir / f"{pdf_path.stem}.json"

    data = extract_pdf_text(pdf_path, extractor=extractor)
    save_extracted_text(data, out_path)
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


def _prepare_request(settings: Settings, pdf_name: str) -> tuple[str, str, str]:
    """Build the (system_prompt, user_prompt, stem) for one datasheet.

    Reads only local step-1 artefacts - no API call - so a batch run can
    prepare and sanity-check every prompt before spending anything.
    """
    stem = Path(pdf_name).stem
    chunks_path = settings.chunks_dir / f"{stem}.json"

    raw_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in raw_chunks]

    system_prompt = load_system_prompt(settings.prompts_dir / "system.txt")
    user_prompt = prepare_user_prompt(
        datasheet_file=Path(pdf_name).name,
        selected_chunks=select_relevant_chunks(chunks),
        schema_markdown=settings.schema_path.read_text(encoding="utf-8"),
    )
    return system_prompt, user_prompt, stem


def _write_profile(settings: Settings, stem: str, response_text: str,
                   backend: str, model: str, effort: str,
                   via_batch: bool = False) -> list[str]:
    """Parse a model response into pdf_output/<stem>.yaml + .meta.json.

    Returns the validation issues; raises if the response is not parseable.
    """
    extracted = json.loads(
        (settings.step1_dir / f"{stem}.json").read_text(encoding="utf-8")
    )
    profile, issues = parse_and_validate(response_text)

    out_path = settings.output_dir / f"{stem}.yaml"
    meta_path = settings.output_dir / f"{stem}.meta.json"
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
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"Wrote {meta_path}")

    if issues:
        print("\nValidation issues:")
        for issue in issues:
            print(f"- {issue}")
    return issues


def cmd_profile(pdf_name: str, backend: str | None = None,
                effort: str | None = None) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    resolved_backend = backend or settings.backend
    resolved_effort = effort or settings.effort
    print(f"  backend: {resolved_backend}")

    system_prompt, user_prompt, stem = _prepare_request(settings, pdf_name)
    llm = make_client(resolved_backend, settings.model_name, effort=resolved_effort)
    response = llm.generate_yaml_profile(system_prompt, user_prompt)
    _write_profile(settings, stem, response, resolved_backend,
                   settings.model_name, resolved_effort)


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
          f"'schematic-extract batch --resume {batch_id}')")
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
            _write_profile(settings, stem, text, backend, model, effort,
                           via_batch=True)
            processed.append(stem)
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
    for pdf_path in pdfs:
        if (settings.output_dir / f"{pdf_path.stem}.yaml").exists() and not force:
            skipped.append(pdf_path.name)
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
                _write_profile(settings, item.stem, response, resolved_backend,
                               settings.model_name, resolved_effort)
                processed.append(item.stem)
            except Exception as exc:
                print(f"  ERROR: {exc}")
                failed.append(item.stem)

    _summary(processed, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schematic-extract",
        description="Extract structured component profiles from datasheet PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("extract-text", help="Step 1: PDF -> raw per-page text (pdf_step1/)")
    p1.add_argument("pdf_name")
    p1.add_argument("--extractor", choices=_EXTRACTOR_CHOICES, default="pymupdf",
                    help="PDF extraction backend (default: pymupdf)")

    p2 = sub.add_parser("chunk", help="Step 2: raw text -> heading-aware chunks (pdf_step1/chunks/)")
    p2.add_argument("pdf_name")

    p3 = sub.add_parser("profile", help="Step 3: chunks -> component profile YAML (pdf_output/)")
    p3.add_argument("pdf_name")
    p3.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="LLM backend (overrides DATASHEET_BACKEND env var)")
    p3.add_argument("--effort", choices=EFFORT_CHOICES,
                    help="Thinking effort, claude backend only "
                         "(overrides DATASHEET_EFFORT; default high)")

    p4 = sub.add_parser("batch", help="Run all three steps for every PDF in pdf_input/ lacking a profile")
    p4.add_argument("--force", action="store_true", help="Reprocess even if a profile already exists")
    p4.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="LLM backend (overrides DATASHEET_BACKEND env var)")
    p4.add_argument("--extractor", choices=_EXTRACTOR_CHOICES, default="pymupdf",
                    help="PDF extraction backend (default: pymupdf)")
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

    p7 = sub.add_parser("doctor", help="Check installation, directories, config, and key safety")
    p7.add_argument("--live", action="store_true",
                    help="Also make one tiny LLM request to verify the key works")

    args = parser.parse_args()

    if args.cmd == "extract-text":
        cmd_extract_text(args.pdf_name, extractor=args.extractor)
    elif args.cmd == "chunk":
        cmd_chunk(args.pdf_name)
    elif args.cmd == "profile":
        cmd_profile(args.pdf_name, backend=args.backend, effort=args.effort)
    elif args.cmd == "batch":
        cmd_batch(force=args.force, backend=args.backend,
                  extractor=args.extractor, effort=args.effort,
                  sync=args.sync, resume=args.resume)
    elif args.cmd == "validate":
        raise SystemExit(cmd_validate(args.profile_path))
    elif args.cmd == "setup":
        from .setup_env import cmd_setup
        cmd_setup(backend=args.backend, model=args.model)
    elif args.cmd == "doctor":
        from .setup_env import cmd_doctor
        raise SystemExit(cmd_doctor(live=args.live))


if __name__ == "__main__":
    main()
