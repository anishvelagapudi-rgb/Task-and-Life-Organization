import json
import logging
import os
import re
from openai import OpenAI
from .provider import AIProvider

logger = logging.getLogger(__name__)

# Observed live (2026-07-14, google/gemma-4-31b-it via NVIDIA NIM, tool_choice="auto",
# both enable_thinking=True and =False -- this is not a thinking-mode-specific quirk,
# and disabling thinking mode did not improve the failure rate: 0/6 across a separate
# 3-trial-each update/delete run, same as thinking=True's small sample): instead of
# populating the real tool_calls field, the model sometimes writes what looks like a
# tool call as plain text in `content`. At least FIVE distinct shapes have shown up
# across two short test runs -- not one stable, quotable format:
#   <|tool_call>call:update_event(end_time='...',event_id='...',start_time='...')<tool_call|>
#   <|tool_call>call:delete_event{event_id: "..."}<tool_call|>
#   <|tool_call>call:update_event{event_id: "...",start_time: "...",end_time: "..."}<tool_call|>
#   <|tool_call>call:update_event({"event_id": "...", "start_time": "...", ...})<tool_call|>
#   <|tool_call>call:update_event({event_id: "...", start_time: "...", ...})<tool_call|>
# Lenient on the exact `<|`/`|>` placement since observed samples disagree with each
# other about which side gets the pipe -- this looks like a broken/partial special-
# token rendering, not something with one fixed shape to anchor on.
_MALFORMED_TOOL_CALL_RE = re.compile(
    r"<\|?tool_call\|?>\s*call:\s*([A-Za-z_]\w*)\s*[\(\{](.*?)[\)\}]\s*<\|?tool_call\|?>",
    re.DOTALL,
)
# Key may or may not be quoted (`event_id:` vs `"event_id":`) -- the wrapping quote
# around the key, if present, is discarded rather than captured; it's the same
# character either side so no backreference is needed for it, unlike the value's.
_MALFORMED_ARG_RE = re.compile(r"['\"]?([A-Za-z_]\w*)['\"]?\s*[:=]\s*(['\"])(.*?)\2")

# The model also hallucinates field names that don't match the real tool schema
# it was given (e.g. `event_id`/`start_time`/`end_time` instead of the actual
# `id`/`start_datetime`/`end_datetime`) -- not just a serialization-format miss but
# a schema miss. This is a fixed, narrow alias table for the specific mismatches
# actually observed, not a general solution to arbitrary future hallucinated names.
_ARG_NAME_ALIASES = {
    "start_time": "start_datetime",
    "end_time": "end_datetime",
}


def _strip_malformed_tool_call_text(content: str) -> str:
    """For plain chat() calls (no `tools` offered at all): confirmed live (see
    nvidia_provider.py module docstring history / README) that the model can still
    emit the same `<|tool_call>call:...<tool_call|>` text even when there is no
    tools schema in the request for it to be attempting to satisfy -- this isn't
    confined to genuine tool-decision uncertainty, it can leak from thinking-mode
    habit into a plain prose-only call. There's no tool_calls list to recover into
    here (chat() callers expect a plain string), so this only ever strips the
    matched block and falls back to an honest message if nothing legible remains
    -- it must never let the raw block reach the user, same principle as
    chat_with_tools's handling below."""
    cleaned = _MALFORMED_TOOL_CALL_RE.sub("", content).strip()
    if cleaned:
        return cleaned
    logger.warning("Stripped tool-call-shaped text from a plain (tools-free) NVIDIA chat() reply: %r", content)
    return "Something went wrong processing that — please try again."


def _recover_malformed_tool_call(content: str, valid_tool_names: set[str]) -> dict | None:
    """Best-effort, fully deterministic (no model call involved) recovery for the
    quirk above. Returns a {"name": ..., "arguments": {...}} dict if `content` matches
    the known malformed shape AND names a real tool from this request's own tool list,
    else None -- callers must treat None as "could not recover" and must never surface
    the raw matched text to the user (see nvidia_provider.py's chat_with_tools)."""
    m = _MALFORMED_TOOL_CALL_RE.search(content)
    if not m:
        return None
    name, raw_args = m.group(1), m.group(2)
    if name not in valid_tool_names:
        return None
    args = {}
    for key, _quote, val in _MALFORMED_ARG_RE.findall(raw_args):
        key = _ARG_NAME_ALIASES.get(key, key)
        # A generic "<anything>_id" -> "id" rule: every write/delete tool's real
        # schema names its target-row parameter literally "id" (see TOOLS in
        # service.py) -- this covers event_id specifically and any sibling
        # task_id/project_id-style hallucination the model might produce the same
        # way, without needing a per-tool-specific table for it.
        if key != "id" and key.endswith("_id"):
            key = "id"
        args[key] = val
    if not args:
        return None
    return {"name": name, "arguments": args}


class NvidiaProvider(AIProvider):
    """
    Talks to models hosted on NVIDIA's API catalog (build.nvidia.com) through its
    OpenAI-compatible endpoint, under the free NVIDIA Developer Program tier —
    this is NVIDIA's hosting, not Google's, even though the default model below
    (gemma-4-31b-it) is a Google release. See README's AI-provider section for
    why this was picked over DeepSeek V4 Pro (known plain-text tool-call bug) and
    the diffusion Gemma variant (unproven tool-calling under a novel, non-
    autoregressive generation architecture).

    Not wired into budget.py: that module's per-token rates are Gemini's specific
    pricing, not a general lookup table, and this endpoint is free under the
    developer program — attributing Gemini's dollar rate to it would just be
    wrong, not conservative. Same reasoning as why groq_provider.py (also
    currently free-tier, also unused as the default) never called budget either.
    """

    DEFAULT_MODEL = "google/gemma-4-31b-it"
    BASE_URL = "https://integrate.api.nvidia.com/v1"

    # NIM/vLLM-specific extension, not a standard OpenAI field — must go through
    # extra_body or the openai SDK never sends it. Enables the model's reasoning
    # ("thinking") mode, which NVIDIA's own docs say tool calling on this Gemma-4
    # family works best under; omitting it would silently leave tool-calling
    # reliability worse than NVIDIA's own reference sample for this exact model.
    _EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": True}}

    def __init__(self, model: str = DEFAULT_MODEL):
        self.client = OpenAI(base_url=self.BASE_URL, api_key=os.environ["NVIDIA_API_KEY"])
        self.model = model

    def chat(self, system: str, messages: list[dict]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            extra_body=self._EXTRA_BODY,
        )
        content = response.choices[0].message.content or ""
        if "tool_call" in content:
            return _strip_malformed_tool_call_text(content)
        return content

    def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            tools=tools,
            tool_choice="auto",
            extra_body=self._EXTRA_BODY,
        )
        choice = response.choices[0]
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_calls = [
                {
                    "call_id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
                for tc in choice.message.tool_calls
            ]
            return choice.message.content or "", tool_calls

        content = choice.message.content or ""
        if "tool_call" in content:
            valid_names = {t["function"]["name"] for t in tools}
            recovered = _recover_malformed_tool_call(content, valid_names)
            if recovered:
                logger.warning(
                    "Recovered malformed NVIDIA tool-call text for %s: %r", recovered["name"], content
                )
                return "", [{"call_id": "recovered_0", **recovered}]
            # Detected the quirk but couldn't parse it into a real call (unknown tool
            # name, or no extractable args) -- never let the raw `<|tool_call>...`
            # text reach the user; that's a strictly worse outcome than an honest
            # "something went wrong."
            logger.warning("Unrecoverable malformed NVIDIA tool-call text: %r", content)
            return "Something went wrong processing that — please try again.", []
        return content, []
