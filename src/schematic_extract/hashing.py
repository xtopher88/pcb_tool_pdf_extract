from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    """Source hash: SHA-256 of the input PDF bytes. Identity of the record."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(data: Any) -> str:
    """Canonical serialization for content hashing: sorted keys, compact
    separators, no embedded timestamps (callers must exclude them)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(data: Any) -> str:
    """Content hash: SHA-256 of the canonicalized output. Same PDF + same
    extractor version should yield the same content hash; a mismatch
    indicates nondeterminism or a hand edit."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
