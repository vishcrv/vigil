"""Gemini client for the agent loop.

`chat_with_tools` is the only interface `agent/loop.py` uses: it takes the loop's own
messages/tools shape, translates it into Gemini's `Content`/`Tool` shape, and translates the
response back. The loop never inspects what comes out beyond `Reply`, so the translation stays
entirely inside this module.
"""

import json
import os
from pathlib import Path
from typing import NamedTuple

import truststore
from dotenv import load_dotenv

# Use the OS's own certificate store instead of the certifi bundle the SDK's HTTP client
# defaults to - some local/corporate networks terminate TLS with a root CA that's in the OS
# store but not in certifi's, which otherwise breaks every live call with a cert-chain error.
truststore.inject_into_ssl()

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MAX_TOKENS = 8000

# Flash-lite, and the alias rather than a pinned version. Every part of this is load-bearing on
# a free-tier key, so don't "tidy" it without re-testing against a real key:
#   - pinned gemini-2.5-flash / -flash-lite    -> 404, "no longer available to new users"
#   - pinned gemini-2.0-flash / -flash-lite    -> 429, limit: 0 (no grant at all on new accounts)
#   - gemini-flash-latest (-> gemini-3.6-flash) -> works, but only 20 requests/DAY free tier, and
#     one analyze() burns ~5 of them in tool-calling round trips = ~4 queries/day. Unusable.
#   - gemini-flash-lite-latest                 -> works, separate + far larger daily bucket, and
#     lower latency, which matters when the loop makes several sequential calls in a live demo.
GEMINI_MODEL = "gemini-flash-lite-latest"

KEY_VAR = "GOOGLE_API_KEY"


class Reply(NamedTuple):
    text: str
    tool_calls: list  # [{"id": str, "name": str, "input": dict}]
    assistant_content: list  # append verbatim to messages as the assistant turn


class ProviderError(RuntimeError):
    """An upstream Gemini API call failed. Keeps SDK exceptions from leaking into the routes,
    so `api/routes/agent.py` can map any upstream failure to one HTTP response.

    `status` is the upstream HTTP status where there was one (429 for quota, etc.), else None.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def check_api_key() -> None:
    """Raise RuntimeError if the Gemini key is missing. Called at FastAPI startup."""
    if not (os.getenv(KEY_VAR) or "").strip():
        raise RuntimeError(
            f"{KEY_VAR} is not set in backend/.env. Get a key from "
            f"https://aistudio.google.com/apikey and add it."
        )


class GeminiClient:
    """Translates the loop's messages/tools into Gemini's shape and back.

    The loop (agent/loop.py) only ever replays `messages` through the SAME client that produced
    them, and never inspects `Reply.assistant_content` itself - it just appends it verbatim to
    `messages` and re-sends the whole list next iteration. That means `assistant_content` only
    has to be something *this* client can parse back into Gemini's `Content` shape on the next
    call. Here it's a small list of plain dicts:
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
                    # name can be recovered - Gemini's FunctionResponse needs it, and the loop's
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
        from google.genai import errors as genai_errors
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=GEMINI_MODEL,
                contents=self._to_gemini_contents(messages),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    tools=self._to_gemini_tools(tools),
                    max_output_tokens=MAX_TOKENS,
                ),
            )
        except genai_errors.APIError as e:
            # Quota exhaustion is a routine condition on a free-tier key, not a crash: one
            # analyze() spends several requests, so a demo can hit the daily cap mid-session.
            # Surface it as something the UI can render instead of an unhandled 500.
            status = getattr(e, "code", None)
            if status == 429:
                raise ProviderError(
                    f"{GEMINI_MODEL} free-tier quota exhausted. Wait for the quota window to "
                    f"reset, or enable billing on the API key's project.",
                    status=429,
                ) from e
            raise ProviderError(f"{GEMINI_MODEL} call failed: {e}", status=status) from e

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


def get_client():
    """Return a client exposing .chat_with_tools(messages, tools, system) -> Reply."""
    check_api_key()  # missing key -> RuntimeError
    return GeminiClient(os.environ[KEY_VAR])
