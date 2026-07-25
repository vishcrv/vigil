"""Provider wrappers for the agent loop. Anthropic is the dev default; Gemini also built (both
translate to/from the loop's Anthropic-shaped messages/tools inside `chat_with_tools`, so the loop
itself stays provider-agnostic). Groq not built yet.
"""

import json
import os
from pathlib import Path
from typing import NamedTuple

import truststore
from dotenv import load_dotenv

# Use the OS's own certificate store instead of the certifi bundle both SDKs' HTTP clients
# default to - some local/corporate networks terminate TLS with a root CA that's in the OS
# store but not in certifi's, which otherwise breaks every live call with a cert-chain error.
truststore.inject_into_ssl()

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Model ID from the `claude-api` skill (current as of 2026-07). Don't guess this.
# Sonnet over Opus deliberately: the loop makes several tool-calling round trips per
# query, so latency is visible during a live demo, and judges run this on their own keys.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000

# Flash over Pro: same "several round trips per query, judges run this live" reasoning as the
# Anthropic model choice above. The alias, not a pinned version: pinned gemini-2.5-flash 404s
# ("no longer available to new users") and pinned gemini-2.0-flash/-lite both 429 with a hard
# zero free-tier quota grant on fresh accounts. "-latest" resolves to whatever flash model the
# account actually has working quota for - confirmed live, don't pin this back without re-testing.
GEMINI_MODEL = "gemini-flash-latest"

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


class GeminiClient:
    """Translates the loop's Anthropic-shaped messages/tools into Gemini's shape and back.

    The loop (agent/loop.py) only ever replays `messages` through the SAME client that produced
    them, and never inspects `Reply.assistant_content` itself — it just appends it verbatim to
    `messages` and re-sends the whole list next iteration. That means `assistant_content` doesn't
    have to be real Anthropic SDK objects; it only has to be something *this* client can parse back
    into Gemini's `Content` shape on the next call. Here it's a small list of plain dicts:
    {"type": "text", "text": ...} or {"type": "tool_use", "id": ..., "name": ..., "input": ...}.
    """

    def __init__(self, api_key: str):
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    def _to_gemini_tools(self, tools: list):
        from google.genai import types

        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters=t["input_schema"],
                    )
                    for t in tools
                ]
            )
        ]

    def _to_gemini_contents(self, messages: list):
        from google.genai import types

        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]

            if isinstance(content, str):
                contents.append(types.Content(role=role, parts=[types.Part(text=content)]))
                continue

            parts = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    parts.append(types.Part(text=block["text"]))
                elif btype == "tool_use":
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(name=block["name"], args=block["input"]),
                            # Gemini's thinking-capable models reject a function_call part on the
                            # next turn if it isn't carrying back the signature issued with it.
                            thought_signature=block.get("thought_signature"),
                        )
                    )
                elif btype == "tool_result":
                    # tool_use_id is "<name>#<i>" (see chat_with_tools below) so the function
                    # name can be recovered — Gemini's FunctionResponse needs it, Anthropic's
                    # tool_result block only carries the opaque id.
                    name = block["tool_use_id"].rsplit("#", 1)[0]
                    try:
                        response = json.loads(block["content"])
                    except (json.JSONDecodeError, TypeError):
                        response = {"result": block["content"]}
                    if not isinstance(response, dict):
                        response = {"result": response}
                    parts.append(
                        types.Part(function_response=types.FunctionResponse(name=name, response=response))
                    )
            contents.append(types.Content(role=role, parts=parts))
        return contents

    def chat_with_tools(self, messages: list, tools: list, system: str) -> Reply:
        from google.genai import types

        response = self._client.models.generate_content(
            model=GEMINI_MODEL,
            contents=self._to_gemini_contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=self._to_gemini_tools(tools),
                max_output_tokens=MAX_TOKENS,
            ),
        )

        candidate_parts = response.candidates[0].content.parts if response.candidates else []
        text_chunks = []
        tool_calls = []
        assistant_content = []
        for i, part in enumerate(candidate_parts):
            if part.text:
                text_chunks.append(part.text)
                assistant_content.append({"type": "text", "text": part.text})
            elif part.function_call:
                call_id = f"{part.function_call.name}#{i}"
                args = dict(part.function_call.args or {})
                tool_calls.append({"id": call_id, "name": part.function_call.name, "input": args})
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": part.function_call.name,
                        "input": args,
                        "thought_signature": part.thought_signature,
                    }
                )

        return Reply(text="".join(text_chunks), tool_calls=tool_calls, assistant_content=assistant_content)


def get_client(provider: str):
    """Return a wrapper exposing .chat_with_tools(messages, tools, system) -> Reply."""
    if provider == "groq":
        raise NotImplementedError(
            "LLM_PROVIDER=groq is not implemented yet: needs the groq SDK in requirements.txt "
            "plus a wrapper translating its function-calling shape to Anthropic-style content "
            "blocks. Use LLM_PROVIDER=anthropic or gemini for now."
        )
    check_api_key(provider)  # unknown provider or missing key -> RuntimeError
    if provider == "gemini":
        return GeminiClient(os.environ[KEY_VARS["gemini"]])
    return AnthropicClient(os.environ[KEY_VARS["anthropic"]])
