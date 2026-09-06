from __future__ import annotations

import os
import json
from dataclasses import dataclass
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import anyio
except ImportError:
    anyio = None

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
except ImportError:
    query = None
    ClaudeAgentOptions = None
    ResultMessage = None


# Opus 5 supports 128K output tokens. The old 16000 ceiling was smaller than
# some profiles we already produce (ADS114S06B's YAML alone is ~15.6K tokens),
# and with adaptive thinking sharing the same budget it truncated mid-YAML.
# All Claude calls stream, so a large ceiling costs nothing in timeout risk.
MAX_OUTPUT_TOKENS = 64000

EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"  # the API default; "medium" is the first cost lever


def first_text_block(response) -> str:
    """Text of the first text block, with a diagnosable error when there is
    none. A bare next() here raised StopIteration, which told you nothing about
    why - truncation and refusal both present as 'no text block'."""
    for block in response.content:
        if block.type == "text":
            return block.text
    reason = getattr(response, "stop_reason", None)
    if reason == "max_tokens":
        raise RuntimeError(
            f"Response hit the {MAX_OUTPUT_TOKENS}-token output ceiling before "
            "emitting any YAML. Narrow the selected chunks or lower --effort."
        )
    if reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise RuntimeError(f"Model declined the request (category: {category}).")
    raise RuntimeError(f"Response contained no text block (stop_reason={reason}).")


@dataclass
class OpenAILLMClient:
    model: str = "gpt-5.4"

    def __post_init__(self) -> None:
        if OpenAI is None:
            raise RuntimeError(
                "openai is not installed. Run: pip install schematic-extract[openai]"
            )
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self.client = OpenAI(api_key=api_key)

    def generate_yaml_profile(self, system_prompt: str, user_prompt: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.output_text

    
    def generate_json_profile(self, system_prompt: str, user_prompt: str) -> dict:
        response = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = response.output_text
        return json.loads(text)


@dataclass
class ClaudeLLMClient:
    model: str = "claude-opus-5"
    effort: str = DEFAULT_EFFORT

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_CHOICES:
            raise ValueError(
                f"effort must be one of {EFFORT_CHOICES}, got {self.effort!r}"
            )
        if anthropic is None:
            raise RuntimeError(
                "anthropic is not installed. Run: pip install anthropic"
            )
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self.client = anthropic.Anthropic(api_key=api_key)

    def request_params(self, system_prompt: str, user_prompt: str) -> dict:
        """The request body shared by the streaming and batch paths, so a
        batched run and a single run send byte-identical prompts."""
        return {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

    def _generate(self, system_prompt: str, user_prompt: str) -> str:
        with self.client.messages.stream(
            **self.request_params(system_prompt, user_prompt)
        ) as stream:
            response = stream.get_final_message()
        return first_text_block(response)

    def generate_yaml_profile(self, system_prompt: str, user_prompt: str) -> str:
        return self._generate(system_prompt, user_prompt)

    def generate_json_profile(self, system_prompt: str, user_prompt: str) -> dict:
        return json.loads(self._generate(system_prompt, user_prompt))


def make_client(backend: str, model_name: str, effort: str = DEFAULT_EFFORT):
    """Factory: return the right LLM client for the given backend name.

    backend values:
      openai       – OpenAI API (requires OPENAI_API_KEY)
      claude       – Anthropic API directly (requires ANTHROPIC_API_KEY)
      claude-code  – Claude Code CLI session via Agent SDK (no API key needed)

    `effort` is honoured by the 'claude' backend only; the others have no
    equivalent knob and ignore it.
    """
    if backend == "claude":
        return ClaudeLLMClient(model=_claude_model(model_name), effort=effort)
    if backend == "claude-code":
        return ClaudeCodeLLMClient(model=_claude_model(model_name))
    return OpenAILLMClient(model=model_name)


def _claude_model(model_name: str) -> str:
    return model_name if "claude" in model_name else "claude-opus-5"


@dataclass
class ClaudeCodeLLMClient:
    """Uses the Claude Code CLI session — no ANTHROPIC_API_KEY required."""
    model: str = "claude-opus-5"

    def _run(self, system_prompt: str, user_prompt: str) -> str:
        if anyio is None:
            raise RuntimeError(
                "anyio is not installed. Run: pip install anyio"
            )
        if query is None or ClaudeAgentOptions is None or ResultMessage is None:
            raise RuntimeError(
                "claude_agent_sdk is not installed. Install the Claude Agent SDK "
                "package that provides the claude_agent_sdk module."
            )

        async def _query() -> str:
            result = ""
            async for message in query(
                prompt=user_prompt,
                options=ClaudeAgentOptions(
                    model=self.model,
                    system_prompt=system_prompt,
                    allowed_tools=[],
                ),
            ):
                if isinstance(message, ResultMessage):
                    result = message.result
            return result
        return anyio.run(_query)

    def generate_yaml_profile(self, system_prompt: str, user_prompt: str) -> str:
        return self._run(system_prompt, user_prompt)

    def generate_json_profile(self, system_prompt: str, user_prompt: str) -> dict:
        text = self._run(system_prompt, user_prompt)
        return json.loads(text)
