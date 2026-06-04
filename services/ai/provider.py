from abc import ABC, abstractmethod


class AIProvider(ABC):
    @abstractmethod
    def chat(self, system: str, messages: list[dict]) -> str:
        """Send a conversation and return the assistant's reply as a string."""
        ...

    @abstractmethod
    def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
        """
        Like chat(), but the model may invoke tools before responding.
        Returns (reply_text, tool_calls) where tool_calls is a list of
        {"call_id": str, "name": str, "arguments": dict}.
        tool_calls is [] when the model chose not to call anything.
        """
        ...
