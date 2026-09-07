"""Anthropic Message Batches support for the profile step.

Batch runs are asynchronous and cost 50% of standard prices. Profiling a
folder of datasheets is inherently non-latency-sensitive, so it is the right
shape for this API: submit every datasheet as one batch, poll, then write the
profiles as results arrive.

A batch can take up to 24 hours, so the submission is recorded in
`<output_dir>/.batches/<batch_id>.json` before polling starts. If polling is
interrupted the work is not lost - `pcb_tool_pdf_extract batch --resume <id>`
picks the same batch back up. Results stay retrievable for 29 days.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
except ImportError:  # anthropic not installed; the claude backend errors first
    MessageCreateParamsNonStreaming = None
    Request = None

STATE_DIRNAME = ".batches"

# Local estimate only, for the end-of-run summary - not authoritative, and only
# used when the model is recognised. USD per 1M tokens at standard rates; batch
# runs are billed at half these.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
BATCH_DISCOUNT = 0.5


@dataclass
class BatchItem:
    """One datasheet's prepared request."""

    stem: str
    system_prompt: str
    user_prompt: str


def state_dir(output_dir: Path) -> Path:
    return output_dir / STATE_DIRNAME


def state_path(output_dir: Path, batch_id: str) -> Path:
    return state_dir(output_dir) / f"{batch_id}.json"


def save_state(output_dir: Path, batch_id: str, mapping: dict[str, str],
               model: str, effort: str) -> Path:
    """Record custom_id -> datasheet stem so a resumed run can route results."""
    path = state_path(output_dir, batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "batch_id": batch_id,
        "model": model,
        "effort": effort,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "mapping": mapping,
    }, indent=2), encoding="utf-8")
    return path


def load_state(output_dir: Path, batch_id: str) -> dict:
    path = state_path(output_dir, batch_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"No record of batch {batch_id} in {state_dir(output_dir)}. "
            "Resume only works for batches this tool submitted."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def pending_batches(output_dir: Path) -> list[dict]:
    """Recorded batches whose results were never fully written."""
    directory = state_dir(output_dir)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def submit(llm, items: list[BatchItem]) -> tuple[str, dict[str, str]]:
    """Create one batch from prepared items.

    Returns (batch_id, {custom_id: stem}). custom_ids are positional rather
    than derived from filenames - stems are not guaranteed to satisfy the
    custom_id charset or the 64-character limit.
    """
    if Request is None or MessageCreateParamsNonStreaming is None:
        raise RuntimeError(
            "anthropic is not installed. Run: pip install -e \".[claude]\""
        )

    requests, mapping = [], {}
    for index, item in enumerate(items):
        custom_id = f"req-{index:04d}"
        mapping[custom_id] = item.stem
        requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                **llm.request_params(item.system_prompt, item.user_prompt)
            ),
        ))

    batch = llm.client.messages.batches.create(requests=requests)
    return batch.id, mapping


def poll_until_done(llm, batch_id: str, interval: int = 30,
                    max_interval: int = 120) -> object:
    """Block until the batch ends, printing progress. Interrupting is safe -
    the batch keeps running server-side and can be resumed by id."""
    delay = interval
    while True:
        batch = llm.client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        counts = batch.request_counts
        print(f"  [{batch.processing_status}] processing={counts.processing} "
              f"succeeded={counts.succeeded} errored={counts.errored}",
              flush=True)
        time.sleep(delay)
        delay = min(delay * 2, max_interval)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Batch-rate cost estimate, or None for an unrecognised model."""
    price = _PRICES.get(model)
    if price is None:
        return None
    in_rate, out_rate = price
    standard = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    return standard * BATCH_DISCOUNT
