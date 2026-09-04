from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _repo_root() -> Path:
    # src/schematic_extract/config.py -> repo root is two levels above the package
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
      PDF_OUTPUT_DIR  component profiles + meta    (default: <workspace>/pdf_output)

    Chunked text (also pre-LLM) lives under <step1>/chunks/.
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
            output_dir=Path(os.getenv("PDF_OUTPUT_DIR", str(workspace / "pdf_output"))),
            prompts_dir=root / "prompts",
            schema_path=root / "schema" / "component_profile_schema.md",
            backend=os.getenv("DATASHEET_BACKEND", "openai"),
            model_name=os.getenv("DATASHEET_MODEL", "gpt-5.4"),
        )


def ensure_dirs(settings: Settings) -> None:
    for path in [
        settings.input_dir,
        settings.step1_dir,
        settings.chunks_dir,
        settings.output_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
