import logging
import os

import httpx

logger = logging.getLogger(__name__)

_CALENDARS_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
_REFRESH_URL = "https://oauth2.googleapis.com/token"

# Single-user local app: process-global cache, refreshed on page load (chat/calendar
# views) rather than fetched live during AI chat turns. The AI is never given a tool
# to pull Google Calendar itself — only this pre-fetched snapshot.
_upcoming_cache = {"events": [], "fetched_at": None}

# Below this age, refresh_upcoming_cache() is a no-op — every chat-view page load
# calls it unconditionally (by design, so the AI's context is never more than one
# navigation stale), but with no throttle at all this meant every single click into
# a chat did a live, synchronous Google Calendar API round trip (one call per
# connected calendar) before the page could render — measured at ~3s per chat open
# even when the cache was seconds old. A single-user personal calendar does not
# change fast enough to need a live refetch on every click within the same minute.
_CACHE_FRESH_SECONDS = 60


def _get_token(db):
    from datetime import datetime, timezone

    row = db.execute("SELECT * FROM tokens WHERE provider = 'google'").fetchone()
    if not row:
        return None
    row = dict(row)
    if row.get("expires_at"):
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (expires_at - datetime.now(timezone.utc)).total_seconds() < 60:
            if not row.get("refresh_token"):
                return None
            try:
                resp = httpx.post(_REFRESH_URL, data={
                    "grant_type": "refresh_token",
                    "refresh_token": row["refresh_token"],
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                }, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                new_token = data["access_token"]
                new_expires = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() + data.get("expires_in", 3600),
                    tz=timezone.utc,
                ).isoformat()
                db.execute(
                    "UPDATE tokens SET access_token = ?, expires_at = ? WHERE provider = 'google'",
                    (new_token, new_expires),
                )
                db.commit()
                return new_token
            except Exception:
                logger.exception("GCal token refresh failed")
                return None
    return row.get("access_token")


def is_connected(db):
    return db.execute("SELECT 1 FROM tokens WHERE provider = 'google'").fetchone() is not None


def list_calendars(db):
    token = _get_token(db)
    if not token:
        return []
    try:
        resp = httpx.get(_CALENDARS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception:
        logger.exception("GCal list_calendars failed")
        return []


def list_events(db, calendar_id, time_min, time_max):
    token = _get_token(db)
    if not token:
        return []
    try:
        url = _EVENTS_URL.format(cal_id=calendar_id)
        resp = httpx.get(url, params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 500,
        }, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception:
        logger.exception("GCal list_events failed for %s", calendar_id)
        return []


def refresh_upcoming_cache(db, days_back=60, days_ahead=60):
    """Fetch events across all connected GCal calendars and stash them for the AI
    chat's passive context injection. Call this on any page load that could lead
    into a chat turn (chat view, calendar view) — never from inside the AI's
    tool-calling loop. Window spans well into the past too, since chat questions
    often ask about recent past dates ("what happened on the 28th").

    No-ops if the cache was refreshed within _CACHE_FRESH_SECONDS — see that
    constant's comment for why (this used to run, unthrottled, on every chat-view
    page load)."""
    from datetime import datetime, timedelta, timezone

    if _upcoming_cache["fetched_at"] is not None:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(_upcoming_cache["fetched_at"])).total_seconds()
        if age < _CACHE_FRESH_SECONDS:
            return

    if not is_connected(db):
        _upcoming_cache["events"] = []
        _upcoming_cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return

    time_min = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    time_max = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()
    events = []
    try:
        for cal in list_calendars(db):
            cal_id = cal["id"]
            for e in list_events(db, cal_id, time_min, time_max):
                start = e.get("start", {})
                end = e.get("end", {})
                events.append({
                    "title": e.get("summary", "(no title)"),
                    "start": start.get("dateTime") or start.get("date"),
                    "end": end.get("dateTime") or end.get("date"),
                    "location": e.get("location"),
                    "calendar_name": cal.get("summary", "Google Calendar"),
                })
    except Exception:
        logger.exception("GCal upcoming cache refresh failed")

    events.sort(key=lambda e: e["start"] or "")
    _upcoming_cache["events"] = events
    _upcoming_cache["fetched_at"] = datetime.now(timezone.utc).isoformat()


def get_cached_upcoming():
    return _upcoming_cache["events"]
