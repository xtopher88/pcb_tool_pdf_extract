from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

from .extract_profile import DEFAULT_MAX_CHARS


def _repo_root() -> Path:
    # src/pcb_tool_pdf_extract/config.py -> repo root is two levels above the package
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Pipeline directories and LLM configuration.

    Directory layout (all overridable via environment variables):

      PDF_WORKSPACE   parent of the four pipeline directories
                      (default: parent of this repo, so the repo sits
                      alongside its data directories)
      PDF_INPUT_DIR   source PDFs                  (default: <workspace>/pdf_input)
      PDF_STEP1_DIR   raw extracted text, pre-LLM  (default: <workspace>/pdf_step1)
      PDF_OUTPUT_DIR  component profiles + meta    (default: <workspace>/pcb_tool_pdf_output)

    Chunked text (also pre-LLM) lives under <step1>/chunks/.

    DATASHEET_MAX_CHARS caps the datasheet text placed in one prompt. The
    default suits a 200K-token context; raise it for a larger one. Selection
    is additive, so this is the only thing that ever drops a chunk.

    DATASHEET_EFFORT tunes how much thinking the model spends per datasheet
    ("low" through "max"; default "high", the API default). It applies to the
    'claude' backend only - the other backends have no equivalent knob.
    """

    repo_root: Path
    workspace: Path
    input_dir: Path
    step1_dir: Path
    chunks_dir: Path
    output_dir: Path
    prompts_dir: Path
    schema_path: Path
    backend: str  # "openai" | "claude" | "claude-code"
    model_name: str
    effort: str  # "low" | "medium" | "high" | "xhigh" | "max" (claude backend only)
    max_chars: int  # datasheet characters allowed in one prompt

    @staticmethod
    def load() -> "Settings":
        root = _repo_root()
        load_dotenv(root / ".env")
        workspace = Path(os.getenv("PDF_WORKSPACE", str(root.parent)))
        step1_dir = Path(os.getenv("PDF_STEP1_DIR", str(workspace / "pdf_step1")))
        return Settings(
            repo_root=root,
            workspace=workspace,
            input_dir=Path(os.getenv("PDF_INPUT_DIR", str(workspace / "pdf_input"))),
            step1_dir=step1_dir,
            chunks_dir=step1_dir / "chunks",
            output_dir=Path(os.getenv("PDF_OUTPUT_DIR", str(workspace / "pcb_tool_pdf_output"))),
            prompts_dir=root / "prompts",
            schema_path=root / "schema" / "component_profile_schema.md",
            backend=os.getenv("DATASHEET_BACKEND", "openai"),
            model_name=os.getenv("DATASHEET_MODEL", "gpt-5.4"),
            effort=os.getenv("DATASHEET_EFFORT", "high"),
            max_chars=int(os.getenv("DATASHEET_MAX_CHARS", str(DEFAULT_MAX_CHARS))),
        )


def ensure_dirs(settings: Settings) -> None:
    for path in [
        settings.input_dir,
        settings.step1_dir,
        settings.chunks_dir,
        settings.output_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
