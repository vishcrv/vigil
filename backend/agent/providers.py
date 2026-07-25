"""Provider wrappers for the agent loop. Anthropic is the dev default and the only one built.

The loop speaks Anthropic-shaped messages/tools (content blocks, `tool_use` / `tool_result`).
A future groq/gemini wrapper translates to/from that shape inside `chat_with_tools`, so the loop
itself stays provider-agnostic.
"""

import os
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Model ID from the `claude-api` skill (current as of 2026-07). Don't guess this.
# Sonnet over Opus deliberately: the loop makes several tool-calling round trips per
# query, so latency is visible during a live demo, and judges run this on their own keys.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000

KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


class Reply(NamedTuple):
    text: str
    tool_calls: list  # [{"id": str, "name": str, "input": dict}]
    assistant_content: list  # append verbatim to messages as the assistant turn


def check_api_key(provider: str) -> None:
    """Raise RuntimeError if the key for `provider` is missing. Called at FastAPI startup."""
    var = KEY_VARS.get(provider)
    if var is None:
        raise RuntimeError(
            f"LLM_PROVIDER={provider!r} is not one of {sorted(KEY_VARS)}. Fix it in backend/.env."
        )
    if not (os.getenv(var) or "").strip():
        raise RuntimeError(
            f"{var} is not set in backend/.env, but LLM_PROVIDER={provider}. "
            f"Add the key, or set LLM_PROVIDER to a provider whose key you have."
        )


class AnthropicClient:
    def __init__(self, api_key: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def chat_with_tools(self, messages: list, tools: list, system: str) -> Reply:
        message = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in message.content
            if b.type == "tool_use"
        ]
        return Reply(text=text, tool_calls=calls, assistant_content=message.content)


def get_client(provider: str):
    """Return a wrapper exposing .chat_with_tools(messages, tools, system) -> Reply."""
    if provider in ("groq", "gemini"):
        raise NotImplementedError(
            f"LLM_PROVIDER={provider} is not implemented yet: needs the {provider} SDK in "
            f"requirements.txt plus a wrapper translating its function-calling shape to "
            f"Anthropic-style content blocks. Use LLM_PROVIDER=anthropic for now."
        )
    check_api_key(provider)  # unknown provider or missing key -> RuntimeError
    return AnthropicClient(os.environ[KEY_VARS["anthropic"]])
