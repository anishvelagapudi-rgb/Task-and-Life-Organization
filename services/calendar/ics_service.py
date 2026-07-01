import logging
import uuid
from datetime import datetime, timezone, date as _date

import httpx
from icalendar import Calendar as ICalendar

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(dt_prop):
    raw = dt_prop.dt if hasattr(dt_prop, "dt") else dt_prop
    if isinstance(raw, _date) and not isinstance(raw, datetime):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc).isoformat(), True
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return raw.isoformat(), False
    return str(raw), False


def store_events(db, calendar_id, ics_bytes):
    cal = ICalendar.from_ical(ics_bytes)
    count = 0
    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        uid = str(comp.get("UID", ""))
        title = str(comp.get("SUMMARY", "Untitled"))
        description = str(comp.get("DESCRIPTION") or "").strip() or None
        location = str(comp.get("LOCATION") or "").strip() or None
        dtstart = comp.get("DTSTART")
        dtend = comp.get("DTEND")
        if not dtstart:
            continue
        start, all_day = _parse_dt(dtstart)
        end = _parse_dt(dtend)[0] if dtend else None

        existing = db.execute(
            "SELECT id FROM events WHERE calendar_id = ? AND source_uid = ?",
            (calendar_id, uid),
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE events
                   SET title=?, description=?, location=?,
                       start_datetime=?, end_datetime=?, all_day=?, updated_at=?
                   WHERE id=?""",
                (title, description, location, start, end, int(all_day), _now(), existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO events
                   (id, calendar_id, title, description, start_datetime, end_datetime,
                    all_day, location, source_uid, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), calendar_id, title, description,
                 start, end, int(all_day), location, uid, _now(), _now()),
            )
            count += 1
    db.commit()
    return count


def fetch_and_store(db, calendar_id, ics_url):
    resp = httpx.get(ics_url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return store_events(db, calendar_id, resp.content)
