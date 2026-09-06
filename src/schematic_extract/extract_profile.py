from __future__ import annotations

from pathlib import Path
import re

from .chunking import Chunk
from .validate_profile import validate_profile


def load_system_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8")


def select_relevant_chunks(chunks: list[Chunk], category_hint: str | None = None) -> list[Chunk]:
    priority = {
        "absolute maximum ratings",
        "recommended operating conditions",
        "electrical characteristics",
        "pin description",
        "pin definitions",
        "register map",
        "register description",
        "power supply recommendations",
        "typical application",
        "layout guidelines",
        "i2c interface",
        "spi interface",
    }
    selected = [c for c in chunks if c.heading_hint in priority]
    return selected if selected else chunks[:8]


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
