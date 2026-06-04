import json
import os
from google import genai
from google.genai import types
from .provider import AIProvider
from . import budget

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
        """
        usage = response.usage_metadata
        budget.record_usage(
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=usage.candidates_token_count or 0,
            model=self.model,
        )

    def chat(self, system: str, messages: list[dict]) -> str:
        budget.check()  # fail fast if budget already blown before hitting the API
        response = self.client.models.generate_content(
            model=self.model,
            contents=_to_gemini_contents(messages),
            config=types.GenerateContentConfig(system_instruction=system),
        )
        self._record(response)  # record actual token usage from the response
        return response.text

    def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        budget.check()  # fail fast if budget already blown before hitting the API
        response = self.client.models.generate_content(
            model=self.model,
            contents=_to_gemini_contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=_convert_tools(tools),
            ),
        )
        self._record(response)  # record actual token usage from the response

        text_parts = []
        tool_calls = []
        for idx, part in enumerate(response.candidates[0].content.parts):
            if part.function_call and part.function_call.name:
                tool_calls.append({
                    "call_id": f"call_{idx}",
                    "name": part.function_call.name,
                    "arguments": dict(part.function_call.args),
                })
            elif part.text:
                text_parts.append(part.text)

        return "".join(text_parts), tool_calls
