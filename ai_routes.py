import hashlib
import hmac
import logging
import os
from flask import Blueprint, jsonify, request
from db import get_db
from services.ai.budget import BudgetExceededError
from services.ai.gemini_provider import GeminiProvider
from services.ai.nvidia_provider import NvidiaProvider
from services.ai.service import AIService, has_pending_delete_marker

logger = logging.getLogger(__name__)

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
        # Same hybrid split as app.py's browser-facing service — Gemini decides/
        # executes every tool call, NvidiaProvider only handles tools-free
        # reasoning/synthesis calls. See AIService.__init__'s docstring.
        _service = AIService(GeminiProvider(), reasoning_provider=NvidiaProvider())
    return _service


@ai_bp.route("/recommendations")
def recommendations():
    """
    GET /api/ai/recommendations
    Returns top 3 recommended tasks + an insight about the task list.
    """
    try:
        result = _get_service().get_recommendations(get_db())
    except BudgetExceededError as e:
        return jsonify({"error": str(e)}), 429
    except Exception:
        # Previously only caught httpx.NetworkError, a Gemini/google-genai-specific
        # exception type — a non-httpx-based provider (e.g. NvidiaProvider, on the
        # openai SDK) failing the same way (empirically: a 504 from NVIDIA's gateway
        # under a large tool-calling round) fell through uncaught, past this
        # blueprint entirely, into Flask's bare default 500 with no JSON body —
        # bad for a REST API contract every response elsewhere here honors.
        logger.exception("AI recommendations failed")
        return jsonify({"error": "AI service unavailable"}), 503
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
    except BudgetExceededError as e:
        return jsonify({"error": str(e)}), 429
    except Exception:
        # See the matching comment in recommendations() above — provider-agnostic on
        # purpose, not narrowed to Gemini's own exception types.
        logger.exception("AI chat failed")
        return jsonify({"error": "AI service unavailable"}), 503
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
