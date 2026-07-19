from datetime import date, datetime, timedelta

from . import query as training_query

# Deterministic pattern-detectors over training_extractions -- no LLM call, per
# this project's own stance on proactive nudges (README "What Is Not Done":
# execution-layer logic, not AI inference). Every detector states a number and a
# comparison, then stops -- no encouragement/warning language. A "logging streak"
# detector (consecutive days with an entry) was deliberately NOT built here: a
# friction-auditor review during planning flagged it as measuring app-usage
# frequency rather than a training signal, the textbook shape of a dark pattern
# regardless of threshold-gating -- see the Phase 2 plan for the full rationale.


def _detect_prs(db) -> dict | None:
    by_exercise = training_query.one_rm_by_exercise(db)
    for exercise, points in by_exercise.items():
        if len(points) < 2:
            continue
        ordered = sorted(points, key=lambda p: p["date"])
        latest = ordered[-1]
        previous_best = max(p["one_rm"] for p in ordered[:-1])
        if latest["one_rm"] > previous_best:
            return {
                "type": "pr",
                "message": f"New PR: {exercise.title()} {latest['one_rm']} lbs (previous best {previous_best} lbs).",
            }
    return None


def _detect_weight_trend(db, today: date) -> dict | None:
    window_start = (today - timedelta(days=14)).isoformat()
    points = training_query.weight_trend(db, date_from=window_start, date_to=today.isoformat())
    if len(points) < 2:
        return None
    ordered = sorted(points, key=lambda p: p["date"])
    delta = round(ordered[-1]["value_lbs"] - ordered[0]["value_lbs"], 1)
    if abs(delta) < 1:
        return None
    direction = "down" if delta < 0 else "up"
    return {
        "type": "weight_trend",
        "message": f"Weight {direction} {abs(delta)} lbs over the last 2 weeks.",
    }


def _detect_mileage_trend(db, today: date) -> dict | None:
    points = training_query.weekly_mileage(db)
    if len(points) < 2:
        return None
    by_week = {p["week"]: p["miles"] for p in points}
    this_year, this_week, _ = today.isocalendar()
    this_label = f"{this_year}-W{this_week:02d}"
    last_date = today - timedelta(days=7)
    last_year, last_week, _ = last_date.isocalendar()
    last_label = f"{last_year}-W{last_week:02d}"
    this_miles, last_miles = by_week.get(this_label), by_week.get(last_label)
    if not this_miles or not last_miles:
        return None
    pct = round((this_miles - last_miles) / last_miles * 100)
    if abs(pct) < 15:
        return None
    direction = "up" if pct > 0 else "down"
    return {
        "type": "mileage_trend",
        "message": f"Weekly mileage {direction} {abs(pct)}% from last week ({last_miles} → {this_miles} mi).",
    }


def _detect_resting_hr_streak(db) -> dict | None:
    points = training_query.metric_series(db, "resting_hr")
    if len(points) < 3:
        return None
    ordered = sorted(points, key=lambda p: p["date"])
    run = [ordered[-1]]
    for p in reversed(ordered[:-1]):
        prev = run[-1]
        expected_prior_day = (date.fromisoformat(prev["date"]) - timedelta(days=1)).isoformat()
        if p["date"] == expected_prior_day and p["value"] < prev["value"]:
            run.append(p)
        else:
            break
    if len(run) < 3:
        return None
    run.reverse()
    return {
        "type": "resting_hr_streak",
        "message": (
            f"Resting heart rate has risen {len(run)} mornings in a row "
            f"({run[0]['value']:g} → {run[-1]['value']:g} bpm)."
        ),
    }


def _detect_soreness_frequency(db, today: date) -> dict | None:
    trailing_start = (today - timedelta(days=6)).isoformat()
    baseline_start = (today - timedelta(days=13)).isoformat()
    baseline_end = (today - timedelta(days=7)).isoformat()
    trailing = training_query.query_rows(
        db, metric_types=["soreness_injury"], date_from=trailing_start, date_to=today.isoformat(), limit=100,
    )
    baseline = training_query.query_rows(
        db, metric_types=["soreness_injury"], date_from=baseline_start, date_to=baseline_end, limit=100,
    )
    if len(trailing) < 2 or len(trailing) <= len(baseline):
        return None
    return {
        "type": "soreness_frequency",
        "message": (
            f"{len(trailing)} soreness/injury mentions in the last 7 days, "
            f"vs. {len(baseline)} the week before."
        ),
    }


def compute_insights(db, today: str | None = None) -> list[dict]:
    today_date = date.fromisoformat(today) if today else datetime.utcnow().date()
    detectors = (
        _detect_prs,
        lambda db: _detect_weight_trend(db, today_date),
        lambda db: _detect_mileage_trend(db, today_date),
        _detect_resting_hr_streak,
        lambda db: _detect_soreness_frequency(db, today_date),
    )
    insights = []
    for detector in detectors:
        result = detector(db)
        if result:
            insights.append(result)
    return insights
