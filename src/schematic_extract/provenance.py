"""Where a datasheet PDF came from.

`source_sha256` identifies a file; without a URL it identifies a file nobody
can find again. This module recovers that URL with no change to the download
workflow: every mainstream browser records the download location in an NTFS
alternate data stream (`Zone.Identifier`) beside the file, so for
already-downloaded PDFs the provenance is usually sitting there already.

That stream is fragile - zip round-trips, FAT/exFAT drives, cloud-sync
clients and git all discard it - so it is harvested at `extract-text` time
and copied into the durable record, never relied on later.

Precedence: an explicit --source-url, then the `sources.yaml` sidecar, then
Zone.Identifier. Nothing is inferred beyond that.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

# Datasheet licences are essentially never machine-readable, and vendor PDFs
# are overwhelmingly all-rights-reserved. Assume the restrictive case; a
# genuinely redistributable datasheet has to opt in via the sidecar.
DEFAULT_LICENSE = "proprietary-unverified"

SIDECAR_NAME = "sources.yaml"

# Pure-tracking query parameters. A denylist, not an allowlist: leaving an
# unnecessary parameter in costs nothing, while stripping a required one
# (LCSC's productCode, Infineon's folderId/fileId) breaks the URL.
_TRACKING_PARAMS = {
    "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "srsltid", "_gl",
    "mkt_tok", "ref_url", "ts", "HQS", "disposition", "elqTrackId",
}

_NUL = chr(0)


@dataclass(frozen=True)
class Provenance:
    source_url: str | None
    publisher: str | None
    retrieved: str | None
    license: str
    url_source: str  # "manual" | "sidecar" | "zone-identifier" | "none"
    url_note: str | None  # set when the URL needs a human look

    def as_meta_fields(self) -> dict:
        return asdict(self)

    @property
    def resolved(self) -> bool:
        return bool(self.source_url)


def read_zone_identifier(pdf_path: Path) -> dict[str, str]:
    """Parse the NTFS Zone.Identifier stream. Empty dict when absent."""
    try:
        with open(f"{pdf_path}:Zone.Identifier", "r",
                  encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return {}  # no stream, non-NTFS filesystem, or not Windows
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            # The stream is NUL-terminated; leaving it in encodes as %00
            # inside the URL and defeats the .pdf suffix check.
            values[key.strip()] = value.replace(_NUL, "").strip()
    return values


def clean_url(url: str) -> tuple[str, list[str]]:
    """Strip tracking parameters. Returns (cleaned, dropped parameter names)."""
    parts = urlsplit(url.strip())
    kept, dropped = [], []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in _TRACKING_PARAMS or key.startswith("utm_"):
            dropped.append(key)
        else:
            kept.append((key, value))
    cleaned = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), "")
    )
    return cleaned, dropped


def load_sidecar(input_dir: Path) -> dict[str, dict]:
    """Read pdf_input/sources.yaml, keyed by source_sha256.

    Keyed by hash rather than filename so an entry survives the PDF being
    renamed - the same reason the skip check keys on content.
    """
    path = input_dir / SIDECAR_NAME
    if not path.is_file():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("sources") or {}
    return {str(k): (v or {}) for k, v in sources.items()} if isinstance(sources, dict) else {}


def _retrieved_date(pdf_path: Path) -> str | None:
    """Download time, approximated by mtime - browsers stamp it on save."""
    try:
        stamp = pdf_path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()


def harvest(pdf_path: Path, source_sha256: str, *,
            source_url: str | None = None,
            sidecar: dict[str, dict] | None = None) -> Provenance:
    """Best available provenance for one PDF."""
    entry = (sidecar or {}).get(source_sha256, {})
    note = None

    if source_url:
        url, origin = source_url, "manual"
    elif entry.get("source_url"):
        url, origin = str(entry["source_url"]), "sidecar"
    else:
        zone = read_zone_identifier(pdf_path)
        # HostUrl is where the bytes came from; ReferrerUrl is the page you
        # were on, which can be a search engine rather than the vendor.
        raw = zone.get("HostUrl") or zone.get("ReferrerUrl") or ""
        url, origin = (raw, "zone-identifier") if raw else ("", "none")

    if not url:
        return Provenance(None, None, _retrieved_date(pdf_path),
                          str(entry.get("license") or DEFAULT_LICENSE),
                          "none", "no URL recorded - add one to sources.yaml")

    url, _dropped = clean_url(url)
    if not urlsplit(url).path.lower().endswith(".pdf"):
        note = "not a direct PDF link - verify before relying on it to re-fetch"

    return Provenance(
        source_url=url,
        publisher=urlsplit(url).netloc or None,
        retrieved=str(entry.get("retrieved") or _retrieved_date(pdf_path)),
        license=str(entry.get("license") or DEFAULT_LICENSE),
        url_source=origin,
        url_note=note,
    )


def sidecar_stub(rows: list[tuple[str, str]]) -> str:
    """A pasteable sources.yaml block for PDFs with no recoverable URL."""
    lines = ["sources:"]
    for source_sha256, filename in rows:
        lines += [
            f"  # {filename}",
            f"  {source_sha256}:",
            "    source_url: \"\"   # <- paste the download URL",
            f"    license: {DEFAULT_LICENSE}",
        ]
    return "\n".join(lines)
