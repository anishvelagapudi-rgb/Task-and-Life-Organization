import hashlib
import hmac
import os
import httpx
from flask import Blueprint, jsonify, request
from db import get_db
from services.ai.gemini_provider import GeminiProvider
from services.ai.service import AIService, has_pending_delete_marker

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")


@ai_bp.before_request
def require_api_key():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    incoming_hash = hashlib.sha512(auth[len("Bearer "):].encode()).hexdigest()
    if not hmac.compare_digest(incoming_hash, os.environ.get("API_KEY_HASH", "")):
        return jsonify({"error": "Unauthorized"}), 401

# Lazily initialized on first request so GROQ_API_KEY is read from the loaded .env,
# not at import time (which happens before load_dotenv() runs in app.py).
_service: AIService | None = None


def _get_service() -> AIService:
    global _service
    if _service is None:
        _service = AIService(GeminiProvider())
    return _service


@ai_bp.route("/recommendations")
def recommendations():
    """
    GET /api/ai/recommendations
    Returns top 3 recommended tasks + an insight about the task list.
    """
    try:
        result = _get_service().get_recommendations(get_db())
    except httpx.NetworkError:
        return jsonify({"error": "AI service unreachable"}), 503
    return jsonify(result)


@ai_bp.route("/chat", methods=["POST"])
def chat():
    """
    POST /api/ai/chat
    Body: {"messages": [{"role": "user", "content": "..."}, ...]}
    Returns: {"reply": "..."}

    The full conversation history is passed in each request (stateless on the server).
    The frontend is responsible for accumulating message history between turns.
    """
    data = request.get_json(force=True) or {}
    messages = data.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages array is required"}), 400

    try:
        reply, sources = _get_service().chat(get_db(), messages)
    except httpx.NetworkError:
        return jsonify({"error": "AI service unreachable"}), 503
    # `reply` intentionally keeps any internal [ref: ...] delete-confirmation marker,
    # unlike app.py's browser route — this endpoint is stateless (see the docstring
    # above), so the caller is what persists/replays message history between turns,
    # and the marker has to survive in *their* copy for the confirmation round trip to
    # work on their next call. `pending_delete` is provided as a convenience so a
    # caller doesn't have to pattern-match the marker itself to know this reply needs a
    # yes/no rather than being treated as a normal answer.
    return jsonify({
        "reply": reply,
        "sources": sources,
        "pending_delete": has_pending_delete_marker(reply),
    })
