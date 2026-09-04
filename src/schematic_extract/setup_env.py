from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from .config import Settings, ensure_dirs

BACKENDS: dict[str, dict] = {
    "openai": {
        "key_var": "OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "package": "openai",
    },
    "claude": {
        "key_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "package": "anthropic",
    },
    "claude-code": {
        "key_var": None,
        "default_model": "claude-sonnet-4-6",
        "package": "claude_agent_sdk",
    },
}

_KEY_VARS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]


def _prompt_secret(prompt: str) -> str:
    """Hidden-input prompt. On Windows, getpass reads the console directly and
    hangs when stdin is redirected, so fall back to plain stdin (piped input,
    e.g. from a CI secret) when not attached to a terminal."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    line = sys.stdin.readline()
    return line.strip()


def mask_key(key: str) -> str:
    """Never print a full key. sk-abc...wxyz style masking."""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _env_file_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            name, value = stripped.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Update NAME=value lines in place, preserving comments and other lines."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip()
            if name in remaining:
                out.append(f"{name}={remaining.pop(name)}")
                continue
        out.append(line)
    for name, value in remaining.items():
        out.append(f"{name}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _git(root: Path, *args: str) -> int:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True,
        ).returncode
    except FileNotFoundError:
        return -1


def check_env_git_safety(root: Path) -> tuple[str, str]:
    """Returns (status, message): status is 'ok', 'warn', or 'fail'."""
    if not (root / ".git").exists():
        return "warn", ".env safety: not a git repository (nothing can be committed)"
    tracked = _git(root, "ls-files", "--error-unmatch", ".env")
    if tracked == 0:
        return "fail", ".env is TRACKED by git - your key would be committed! Run: git rm --cached .env"
    ignored = _git(root, "check-ignore", "-q", ".env")
    if ignored == 0:
        return "ok", ".env is gitignored (keys stay local)"
    if ignored == -1:
        return "warn", ".env safety: git not found, could not verify .gitignore"
    return "fail", ".env is NOT gitignored - add '.env' to .gitignore before committing"


def cmd_setup(backend: str | None = None, model: str | None = None) -> None:
    settings = Settings.load()
    env_path = settings.repo_root / ".env"

    print("schematic-extract setup")
    print(f"  config file: {env_path}")
    print("  (keys are stored only in this file; it is gitignored and never printed)\n")

    if backend is None:
        names = list(BACKENDS)
        print("Which LLM backend should the 'profile' step use?")
        for i, name in enumerate(names, 1):
            info = BACKENDS[name]
            note = (
                f"needs {info['key_var']}"
                if info["key_var"]
                else "no API key needed (uses your Claude Code session)"
            )
            print(f"  {i}. {name:<12} {note}")
        while backend is None:
            try:
                choice = input(f"Choose [1-{len(names)}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nSetup cancelled.")
                return
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                backend = names[int(choice) - 1]
            elif choice in BACKENDS:
                backend = choice

    info = BACKENDS[backend]
    model = model or info["default_model"]
    updates = {"DATASHEET_BACKEND": backend, "DATASHEET_MODEL": model}

    key_var = info["key_var"]
    if key_var:
        in_env_file = _env_file_values(env_path).get(key_var, "")
        ambient = os.environ.get(key_var, "")
        current = in_env_file or ambient
        try:
            if current:
                where = ".env" if in_env_file else "your environment"
                print(f"{key_var} already set in {where} ({mask_key(current)}).")
                entered = _prompt_secret("Press Enter to keep it, or paste a new key: ")
            else:
                entered = _prompt_secret(f"Paste your {key_var} (input hidden): ")
        except (EOFError, KeyboardInterrupt):
            entered = ""
            print()
        if entered:
            updates[key_var] = entered
        elif not current:
            print(f"  No key entered - re-run setup or set {key_var} in your environment later.")

    update_env_file(env_path, updates)
    print(f"\nWrote {env_path}  (backend={backend}, model={model})")

    status, message = check_env_git_safety(settings.repo_root)
    prefix = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}[status]
    print(f"{prefix} {message}")

    print("\nNext: schematic-extract doctor          (check the installation)")
    print("      schematic-extract doctor --live   (also test the LLM connection)")


def cmd_doctor(live: bool = False) -> int:
    # Capture which keys came from the ambient environment before .env is loaded.
    ambient = {var: var in os.environ for var in _KEY_VARS}

    counts = {"fail": 0, "warn": 0}

    def ok(msg: str) -> None:
        print(f"  [ ok ] {msg}")

    def warn(msg: str) -> None:
        counts["warn"] += 1
        print(f"  [warn] {msg}")

    def fail(msg: str) -> None:
        counts["fail"] += 1
        print(f"  [FAIL] {msg}")

    print("schematic-extract doctor\n")

    # -- Python & required packages ------------------------------------
    if sys.version_info >= (3, 11):
        ok(f"Python {sys.version.split()[0]}")
    else:
        fail(f"Python {sys.version.split()[0]} - 3.11+ required")

    for module, why in [("pymupdf", "PDF text extraction"),
                        ("yaml", "profile output"),
                        ("dotenv", ".env loading")]:
        try:
            __import__(module)
            ok(f"{module} installed ({why})")
        except ImportError:
            fail(f"{module} missing ({why}) - pip install -e .")

    try:
        __import__("docling")
        ok("docling installed (OCR fallback for scanned PDFs)")
    except ImportError:
        warn("docling not installed - '--extractor docling/auto' unavailable "
             "(pip install -e \".[docling]\"); fine for most datasheets")

    # -- Directories ----------------------------------------------------
    settings = Settings.load()
    try:
        ensure_dirs(settings)
        ok(f"input dir   {settings.input_dir}")
        ok(f"step1 dir   {settings.step1_dir}")
        ok(f"output dir  {settings.output_dir}")
    except OSError as exc:
        fail(f"cannot create pipeline directories: {exc}")

    # -- Key safety -----------------------------------------------------
    env_path = settings.repo_root / ".env"
    status, message = check_env_git_safety(settings.repo_root)
    {"ok": ok, "warn": warn, "fail": fail}[status](message)

    example = settings.repo_root / ".env.example"
    if example.exists():
        leaked = [name for name, value in _env_file_values(example).items()
                  if "KEY" in name and value]
        if leaked:
            fail(f".env.example has a non-empty value for {', '.join(leaked)} - "
                 "example files are committed; move real keys to .env")
        else:
            ok(".env.example contains no key values")

    # -- Backend configuration -----------------------------------------
    if not env_path.exists():
        warn(f"no .env file yet - using defaults; run 'schematic-extract setup'")

    backend = settings.backend
    if backend not in BACKENDS:
        fail(f"DATASHEET_BACKEND={backend!r} is not one of {list(BACKENDS)}")
        return 1
    ok(f"backend {backend}, model {settings.model_name}")

    info = BACKENDS[backend]
    try:
        __import__(info["package"])
        ok(f"{info['package']} installed (required by backend '{backend}')")
    except ImportError:
        fail(f"{info['package']} not installed - pip install -e \".[{backend}]\"")

    key_var = info["key_var"]
    if key_var:
        value = os.environ.get(key_var, "")
        if value:
            source = "environment" if ambient[key_var] else ".env"
            ok(f"{key_var} set from {source} ({mask_key(value)})")
        else:
            (fail if live else warn)(
                f"{key_var} not set - run 'schematic-extract setup'")
    else:
        ok("backend needs no API key (uses your Claude Code session)")

    # -- Live LLM round-trip -------------------------------------------
    if live and counts["fail"] == 0:
        from .llm_client import make_client
        print("\n  testing LLM connection (one tiny request)...")
        try:
            llm = make_client(backend, settings.model_name)
            response = llm.generate_yaml_profile(
                "You are a connectivity test. Reply with exactly: ok", "ping")
            if response and response.strip():
                ok(f"live round-trip succeeded ({backend}/{settings.model_name})")
            else:
                fail("LLM returned an empty response")
        except Exception as exc:
            fail(f"live round-trip failed: {exc}")
    elif live:
        print("\n  skipping --live test until failures above are fixed")

    # -- Summary --------------------------------------------------------
    print()
    if counts["fail"]:
        print(f"FAILED: {counts['fail']} problem(s), {counts['warn']} warning(s)")
        return 1
    if counts["warn"]:
        print(f"OK with {counts['warn']} warning(s)")
    else:
        print("All checks passed" + ("" if live else " - add --live to test the LLM connection"))
    return 0
