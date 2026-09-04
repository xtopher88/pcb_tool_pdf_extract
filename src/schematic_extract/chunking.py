from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
from pathlib import Path


IMPORTANT_HEADINGS = [
    "absolute maximum ratings",
    "recommended operating conditions",
    "electrical characteristics",
    "pin description",
    "pin definitions",
    "functional description",
    "register map",
    "register description",
    "application information",
    "typical application",
    "layout guidelines",
    "power supply recommendations",
    "timing characteristics",
    "i2c interface",
    "spi interface",
]


@dataclass
class Chunk:
    chunk_id: str
    page_start: int
    page_end: int
    heading_hint: str
    text: str


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).lower()


def detect_heading_hint(page_text: str) -> str:
    lines = [normalize_line(x) for x in page_text.splitlines() if x.strip()]
    for line in lines[:20]:
        for heading in IMPORTANT_HEADINGS:
            if heading in line:
                return heading
    return "general"


def chunk_pages(extracted: dict, max_chars: int = 12000) -> list[Chunk]:
    chunks: list[Chunk] = []
    current_text: list[str] = []
    page_start = 1
    page_end = 1
    current_heading = "general"
    idx = 1

    for page in extracted["pages"]:
        text = page["text"]
        if not text:
            continue

        heading = detect_heading_hint(text)
        proposed = "\n\n".join(current_text + [f"[Page {page['page_num']}]\n{text}"])

        should_flush = (
            len(proposed) > max_chars
            or (heading != "general" and current_text)
        )

        if should_flush:
            chunks.append(Chunk(
                chunk_id=f"chunk_{idx:03d}",
                page_start=page_start,
                page_end=page_end,
                heading_hint=current_heading,
                text="\n\n".join(current_text),
            ))
            idx += 1
            current_text = []
            page_start = page["page_num"]
            current_heading = heading

        if not current_text:
            page_start = page["page_num"]
            current_heading = heading

        current_text.append(f"[Page {page['page_num']}]\n{text}")
        page_end = page["page_num"]

    if current_text:
        chunks.append(Chunk(
            chunk_id=f"chunk_{idx:03d}",
            page_start=page_start,
            page_end=page_end,
            heading_hint=current_heading,
            text="\n\n".join(current_text),
        ))

    return chunks


def save_chunks(chunks: list[Chunk], out_path: Path) -> None:
    payload = [asdict(c) for c in chunks]
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")