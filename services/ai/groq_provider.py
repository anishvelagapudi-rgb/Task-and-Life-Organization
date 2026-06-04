import json
import os
from groq import Groq
from .provider import AIProvider


class GroqProvider(AIProvider):
    DEFAULT_MODEL = "llama-3.3-70b-versatile"

    def __init__(self, model: str = DEFAULT_MODEL):
        self.client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.model = model

    def chat(self, system: str, messages: list[dict]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
        )
        return response.choices[0].message.content

    def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            tools=tools,
            tool_choice="auto",
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
        return choice.message.content or "", []
