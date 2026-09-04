from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import yaml

from . import __version__
from .chunking import Chunk, chunk_pages, save_chunks
from .config import Settings, ensure_dirs
from .extract_profile import (
    extract_and_validate,
    load_system_prompt,
    select_relevant_chunks,
)
from .hashing import content_sha256
from .llm_client import make_client
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


def cmd_profile(pdf_name: str, backend: str | None = None) -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    stem = Path(pdf_name).stem
    raw_path = settings.step1_dir / f"{stem}.json"
    chunks_path = settings.chunks_dir / f"{stem}.json"
    system_prompt_path = settings.prompts_dir / "system.txt"
    out_path = settings.output_dir / f"{stem}.yaml"
    meta_path = settings.output_dir / f"{stem}.meta.json"

    extracted = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in raw_chunks]

    selected = select_relevant_chunks(chunks)
    system_prompt = load_system_prompt(system_prompt_path)
    schema_markdown = settings.schema_path.read_text(encoding="utf-8")

    resolved_backend = backend or settings.backend
    print(f"  backend: {resolved_backend}")
    llm = make_client(resolved_backend, settings.model_name)
    profile, issues = extract_and_validate(
        llm_client=llm,
        system_prompt=system_prompt,
        datasheet_file=Path(pdf_name).name,
        selected_chunks=selected,
        schema_markdown=schema_markdown,
    )

    out_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    meta = {
        "source_file": extracted["file"],
        "source_sha256": extracted["source_sha256"],
        "step1_content_sha256": extracted["content_sha256"],
        "profile_content_sha256": content_sha256(profile),
        "extractor_version": __version__,
        "pdf_extractor": extracted.get("extractor", "pymupdf"),
        "llm_backend": resolved_backend,
        "model": settings.model_name,
        "date": date.today().isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"Wrote {meta_path}")

    if issues:
        print("\nValidation issues:")
        for issue in issues:
            print(f"- {issue}")


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


def cmd_batch(force: bool = False, backend: str | None = None, extractor: str = "pymupdf") -> None:
    settings = Settings.load()
    ensure_dirs(settings)

    pdfs = sorted(settings.input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {settings.input_dir}")
        return

    skipped, processed, failed = [], [], []

    for pdf_path in pdfs:
        out_path = settings.output_dir / f"{pdf_path.stem}.yaml"
        if out_path.exists() and not force:
            skipped.append(pdf_path.name)
            continue

        print(f"\n-- {pdf_path.name} --")
        try:
            print("  [1/3] extract-text")
            cmd_extract_text(pdf_path.name, extractor=extractor)
            print("  [2/3] chunk")
            cmd_chunk(pdf_path.name)
            print("  [3/3] profile")
            cmd_profile(pdf_path.name, backend=backend)
            processed.append(pdf_path.name)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed.append(pdf_path.name)

    print("\n-- Summary --")
    print(f"  Processed : {len(processed)}")
    print(f"  Skipped   : {len(skipped)}")
    print(f"  Failed    : {len(failed)}")
    if skipped:
        print("  (use --force to reprocess skipped)")
    for name in failed:
        print(f"  FAILED: {name}")


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

    p4 = sub.add_parser("batch", help="Run all three steps for every PDF in pdf_input/ lacking a profile")
    p4.add_argument("--force", action="store_true", help="Reprocess even if a profile already exists")
    p4.add_argument("--backend", choices=_BACKEND_CHOICES,
                    help="LLM backend (overrides DATASHEET_BACKEND env var)")
    p4.add_argument("--extractor", choices=_EXTRACTOR_CHOICES, default="pymupdf",
                    help="PDF extraction backend (default: pymupdf)")

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
        cmd_profile(args.pdf_name, backend=args.backend)
    elif args.cmd == "batch":
        cmd_batch(force=args.force, backend=args.backend, extractor=args.extractor)
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
