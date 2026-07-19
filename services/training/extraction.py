import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Caps how many entries go into a single extraction call. README documents real
# batching failures once a single tool-calling round gets large (the
# parent_task_id-as-title-string bug at 50 subtasks, a 504 gateway timeout at
# the same size) -- 30 stays comfortably inside sizes already verified safe in
# this codebase, and since extraction is lazy-on-read, any remainder just gets
# picked up on the next read instead of needing to fit in one call.
_BATCH_SIZE = 30

_VALID_METRIC_TYPES = {
    "weight", "body_measurement", "nutrition", "sleep", "resting_hr",
    "run", "workout_set", "soreness_injury", "mood_energy", "recovery",
    "steps", "note",
}


def extract_pending(db, ai_service) -> int:
    """Lazy-on-read extraction trigger -- mirrors db.py's reset_due_recurring_tasks:
    runs on the read path (called from the /training/dashboard route), no
    scheduler, no button. Selects entries not yet processed, asks the AI to
    extract structured metrics from them, validates the result before writing
    (never trusts a model-returned id blindly -- see db.py's enforce_parent_exists
    docstring for why that discipline exists in this codebase), and marks every
    entry in the batch processed regardless of whether it yielded any
    extractions. Returns the number of entries processed (0 if nothing was pending,
    which skips the AI call entirely).
    """
    rows = db.execute(
        """SELECT id, entry_date, content FROM training_entries
           WHERE processed = 0 ORDER BY created_at LIMIT ?""",
        (_BATCH_SIZE,),
    ).fetchall()
    if not rows:
        return 0

    entries = [dict(r) for r in rows]
    entry_ids = {e["id"] for e in entries}
    entry_date_by_id = {e["id"]: e["entry_date"] for e in entries}

    payload = [{"id": e["id"], "content": e["content"]} for e in entries]
    try:
        raw_items = ai_service.extract_training_metrics(payload)
        if not raw_items:
            # Gemini occasionally returns a malformed/empty response (0 output
            # tokens, MALFORMED_FUNCTION_CALL, etc. -- documented throughout this
            # codebase as a real, observed quirk); gemini_provider.py degrades
            # that to a clean empty result rather than raising, which is
            # indistinguishable here from "genuinely nothing extractable in this
            # batch." Surfaced empirically: a real batch of clearly-extractable
            # entries ("Weight this morning: 180", "Bench 185x5x3"...) got marked
            # processed with zero extractions on the first attempt. One bounded
            # retry (never more) catches exactly this transient case without
            # risking a retry loop.
            logger.warning("Training journal extraction returned 0 items for %d entries, retrying once", len(entries))
            raw_items = ai_service.extract_training_metrics(payload)
    except Exception:
        # A real API/network failure -- leave every entry in this batch unprocessed
        # so the next dashboard load retries them, rather than marking them
        # processed and silently losing whatever they contained. Distinct from a
        # successful call that legitimately extracts nothing (handled below,
        # where entries DO get marked processed).
        logger.exception("Training journal extraction call failed for %d pending entries", len(entries))
        return 0

    model_name = getattr(ai_service.provider, "model", ai_service.provider.__class__.__name__)
    now = datetime.now(timezone.utc).isoformat()

    valid_items = []
    for item in raw_items:
        source_entry_id = item.get("source_entry_id")
        metric_type = item.get("metric_type")
        data = item.get("data")
        confidence = item.get("confidence")
        if source_entry_id not in entry_ids:
            logger.warning("Dropping extraction with unknown source_entry_id=%r", source_entry_id)
            continue
        if metric_type not in _VALID_METRIC_TYPES:
            logger.warning("Dropping extraction with invalid metric_type=%r", metric_type)
            continue
        if not isinstance(data, dict):
            logger.warning("Dropping extraction with non-object data for entry=%r", source_entry_id)
            continue
        if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            confidence = 0.5  # lenient default rather than dropping an otherwise-valid item
        valid_items.append({
            "source_entry_id": source_entry_id,
            "metric_type": metric_type,
            "data": data,
            "confidence": float(confidence),
        })

    for item in valid_items:
        db.execute(
            """INSERT INTO training_extractions
               (id, source_entry_id, entry_date, metric_type, data, confidence,
                extraction_model, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                item["source_entry_id"],
                entry_date_by_id[item["source_entry_id"]],
                item["metric_type"],
                json.dumps(item["data"]),
                item["confidence"],
                model_name,
                now,
            ),
        )

    placeholders = ", ".join("?" * len(entries))
    db.execute(
        f"UPDATE training_entries SET processed = 1 WHERE id IN ({placeholders})",
        [e["id"] for e in entries],
    )
    db.commit()
    return len(entries)
