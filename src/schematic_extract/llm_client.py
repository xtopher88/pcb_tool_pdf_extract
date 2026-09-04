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
    model: str = "claude-sonnet-4-6"
    #"claude-opus-4-6"

    def __post_init__(self) -> None:
        if anthropic is None:
            raise RuntimeError(
                "anthropic is not installed. Run: pip install anthropic"
            )
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate_yaml_profile(self, system_prompt: str, user_prompt: str) -> str:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            response = stream.get_final_message()
        return next(b.text for b in response.content if b.type == "text")

    def generate_json_profile(self, system_prompt: str, user_prompt: str) -> dict:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            response = stream.get_final_message()
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


def make_client(backend: str, model_name: str):
    """Factory: return the right LLM client for the given backend name.

    backend values:
      openai       – OpenAI API (requires OPENAI_API_KEY)
      claude       – Anthropic API directly (requires ANTHROPIC_API_KEY)
      claude-code  – Claude Code CLI session via Agent SDK (no API key needed)
    """
    if backend == "claude":
        return ClaudeLLMClient(model=model_name if "claude" in model_name else "claude-sonnet-4-6")
    if backend == "claude-code":
        return ClaudeCodeLLMClient(model=model_name if "claude" in model_name else "claude-sonnet-4-6")
    return OpenAILLMClient(model=model_name)


@dataclass
class ClaudeCodeLLMClient:
    """Uses the Claude Code CLI session — no ANTHROPIC_API_KEY required."""
    model: str = "claude-sonnet-4-6"

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
