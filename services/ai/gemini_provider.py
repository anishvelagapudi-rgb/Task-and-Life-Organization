import json
import logging
import os
import random
import time
from google import genai
from google.genai import types
from .provider import AIProvider
from . import budget

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds; doubles each attempt, plus 0–1 s jitter


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in _RETRYABLE_STATUS:
        return True
    try:
        return int(str(exc).split()[0]) in _RETRYABLE_STATUS
    except (ValueError, IndexError):
        return False


def _with_retry(fn):
    """Call fn(), retrying up to _MAX_RETRIES times on transient 5xx/429 errors."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc) or attempt == _MAX_RETRIES:
                raise
            delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(
                "Transient API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, _MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)

_TYPE_MAP = {
    "string":  types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number":  types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
    "object":  types.Type.OBJECT,
    "array":   types.Type.ARRAY,
}


def _convert_schema(schema: dict) -> types.Schema:
    kwargs = {}
    if "type" in schema:
        kwargs["type"] = _TYPE_MAP.get(schema["type"], types.Type.STRING)
    if "description" in schema:
        kwargs["description"] = schema["description"]
    if "properties" in schema:
        kwargs["properties"] = {k: _convert_schema(v) for k, v in schema["properties"].items()}
    if "required" in schema:
        kwargs["required"] = schema["required"]
    if "enum" in schema:
        kwargs["enum"] = schema["enum"]
    if "items" in schema:
        kwargs["items"] = _convert_schema(schema["items"])
    return types.Schema(**kwargs)


def _convert_tools(tools: list[dict]) -> list[types.Tool]:
    declarations = []
    for tool in tools:
        fn = tool["function"]
        declarations.append(types.FunctionDeclaration(
            name=fn["name"],
            description=fn.get("description", ""),
            parameters=_convert_schema(fn["parameters"]),
        ))
    return [types.Tool(function_declarations=declarations)]


def _to_gemini_contents(messages: list[dict]) -> list[types.Content]:
    """
    Convert OpenAI-style messages to Gemini Content objects.

    OpenAI: user / assistant / tool
    Gemini: user / model
    Tool results (role=tool) become user-role Contents with FunctionResponse parts.
    Consecutive tool results are merged into one user Content.
    """
    call_id_to_name: dict[str, str] = {}
    for msg in messages:
        for tc in msg.get("tool_calls") or []:
            call_id_to_name[tc["id"]] = tc["function"]["name"]

    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role == "user":
            content = msg.get("content") or ""
            if content:
                result.append(types.Content(role="user", parts=[types.Part(text=content)]))
            i += 1

        elif role == "assistant":
            parts = []
            if msg.get("content"):
                parts.append(types.Part(text=msg["content"]))
            for tc in msg.get("tool_calls") or []:
                parts.append(types.Part(
                    function_call=types.FunctionCall(
                        name=tc["function"]["name"],
                        args=json.loads(tc["function"]["arguments"]),
                    )
                ))
            if parts:
                result.append(types.Content(role="model", parts=parts))
            i += 1

        elif role == "tool":
            parts = []
            while i < len(messages) and messages[i]["role"] == "tool":
                tm = messages[i]
                parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=call_id_to_name.get(tm["tool_call_id"], "unknown"),
                        response=json.loads(tm["content"]),
                    )
                ))
                i += 1
            result.append(types.Content(role="user", parts=parts))

        else:
            i += 1

    return result


class GeminiProvider(AIProvider):
    DEFAULT_MODEL = "gemini-2.5-flash-lite"  # cheapest verified model with function calling support

    def __init__(self, model: str = DEFAULT_MODEL):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model

    def _record(self, response) -> None:
        """
        Pull token counts out of the API response and hand them to the budget tracker.
        response.usage_metadata is populated by Gemini on every generate_content call —
        it reflects the actual tokens processed, not an estimate.

        prompt_token_count/candidates_token_count alone undercount the call: Gemini also
        bills tool_use_prompt_token_count (function-declaration overhead, part of input)
        and thoughts_token_count (internal reasoning tokens, billed as output) separately,
        so both are folded in here.
        """
        usage = response.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
        output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        budget.record_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self.model,
        )

    def chat(self, system: str, messages: list[dict]) -> str:
        budget.check()
        contents = _to_gemini_contents(messages)
        config = types.GenerateContentConfig(system_instruction=system)
        response = _with_retry(lambda: self.client.models.generate_content(
            model=self.model, contents=contents, config=config,
        ))
        self._record(response)
        return response.text

    def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        budget.check()
        contents = _to_gemini_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=_convert_tools(tools),
        )
        response = _with_retry(lambda: self.client.models.generate_content(
            model=self.model, contents=contents, config=config,
        ))
        self._record(response)

        text_parts = []
        tool_calls = []
        # response.candidates[0].content can be None on a finish_reason other than
        # STOP (SAFETY/RECITATION/MALFORMED_FUNCTION_CALL/etc, or the same "0 output
        # tokens" quirk documented elsewhere in this codebase for the round right
        # after a tool call) -- callers already treat "no tool calls, no text" as a
        # valid empty response (see chat()'s _synthesize_tool_confirmation fallback),
        # so degrade to that instead of an AttributeError crashing the whole call.
        content = response.candidates[0].content
        if content is None:
            logger.warning(
                "Gemini chat_with_tools returned no content (finish_reason=%s)",
                getattr(response.candidates[0], "finish_reason", None),
            )
            return "", []
        for idx, part in enumerate(content.parts or []):
            if part.function_call and part.function_call.name:
                tool_calls.append({
                    "call_id": f"call_{idx}",
                    "name": part.function_call.name,
                    "arguments": dict(part.function_call.args),
                })
            elif part.text:
                text_parts.append(part.text)

        return "".join(text_parts), tool_calls
