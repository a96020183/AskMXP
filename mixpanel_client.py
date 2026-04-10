"""Mixpanel API client shared by app.py and monitor.py.

Strategy:
- Primary path = Raw Export API (data.mixpanel.com/api/2.0/export)
  Works on every Mixpanel plan (Export is a separate product from Query API).
  We aggregate events client-side to produce totals / unique users / retention.
- Fallback path = Query API (Segmentation / Retention endpoints)
  Faster and pre-aggregated, but some plans return 402 Payment Required.

Numbers from Export-based aggregation match Mixpanel UI as long as Mixpanel UI
is counting the same thing (same event name, same time zone = UTC, same distinct_id).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Iterator, Optional

import pandas as pd
import requests


# ─────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────
def _auth_headers(project_id: str) -> dict:
    return {
        "Accept": "application/json",
        "X-Mixpanel-Project-Id": project_id,
    }


# ─────────────────────────────────────────────────────────────
# Export API streaming
# ─────────────────────────────────────────────────────────────
def _stream_events(username: str, secret: str, project_id: str,
                   event_name: str, from_date: date, to_date: date) -> Iterator[dict]:
    """Yield each raw event from Mixpanel Export API line by line."""
    url = "https://data.mixpanel.com/api/2.0/export"
    params = {
        "project_id": project_id,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "event": json.dumps([event_name]),
    }
    resp = requests.get(
        url, params=params,
        auth=(username, secret),
        headers=_auth_headers(project_id),
        timeout=120,
        stream=True,
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _bucket_key(d: date, unit: str) -> str:
    """Floor a date to the start of its day/week/month bucket."""
    if unit == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if unit == "month":
        return d.replace(day=1).isoformat()
    return d.isoformat()


# ─────────────────────────────────────────────────────────────
# Export-based aggregation (primary path)
# ─────────────────────────────────────────────────────────────
def _aggregate_from_export(username: str, secret: str, project_id: str,
                           event_name: str, from_date: date, to_date: date,
                           metric: str = "total", unit: str = "day") -> pd.DataFrame:
    """Pull raw events and aggregate into a daily/weekly/monthly time series.

    metric: "total"  → event count
            "unique" → unique distinct_id count
    """
    if metric == "unique":
        bucket_users: dict[str, set] = defaultdict(set)
        for obj in _stream_events(username, secret, project_id, event_name, from_date, to_date):
            props = obj.get("properties", {}) or {}
            did = props.get("distinct_id")
            ts = props.get("time")
            if did is None or ts is None:
                continue
            d = datetime.utcfromtimestamp(ts).date()
            bucket_users[_bucket_key(d, unit)].add(did)
        rows = [{"日期": k, "事件次數": len(v)} for k, v in sorted(bucket_users.items())]
    else:
        bucket_counts: dict[str, int] = defaultdict(int)
        for obj in _stream_events(username, secret, project_id, event_name, from_date, to_date):
            props = obj.get("properties", {}) or {}
            ts = props.get("time")
            if ts is None:
                continue
            d = datetime.utcfromtimestamp(ts).date()
            bucket_counts[_bucket_key(d, unit)] += 1
        rows = [{"日期": k, "事件次數": v} for k, v in sorted(bucket_counts.items())]

    if not rows:
        return pd.DataFrame(columns=["日期", "事件次數"])
    df = pd.DataFrame(rows)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


# ─────────────────────────────────────────────────────────────
# Segmentation API fallback
# ─────────────────────────────────────────────────────────────
def _segmentation(username: str, secret: str, project_id: str,
                  event_name: str, from_date: date, to_date: date,
                  seg_type: str = "general", unit: str = "day") -> pd.DataFrame:
    """Fallback for plans that have Query API access."""
    url = "https://mixpanel.com/api/2.0/segmentation"
    params = {
        "project_id": project_id,
        "event": event_name,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "type": seg_type,
        "unit": unit,
    }
    resp = requests.get(
        url, params=params,
        auth=(username, secret),
        headers=_auth_headers(project_id),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    values = data.get("data", {}).get("values", {})
    if not values:
        return pd.DataFrame(columns=["日期", "事件次數"])

    series = list(values.values())[0]
    rows = [{"日期": k, "事件次數": v} for k, v in sorted(series.items())]
    df = pd.DataFrame(rows)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


# ─────────────────────────────────────────────────────────────
# Public: fetch_event_data
# ─────────────────────────────────────────────────────────────
def fetch_event_data(username: str, secret: str, project_id: str,
                     event_name: str, days: int = 30,
                     metric: str = "total", unit: str = "day") -> pd.DataFrame:
    """Fetch a time series for a single event.

    Args:
        metric: "total" | "unique"
        unit:   "day" | "week" | "month"
        days:   how far back to look
    """
    today = datetime.utcnow().date()
    from_date = today - timedelta(days=days)

    errors: list[str] = []

    # Primary: Export API + client-side aggregation (works on all plans)
    try:
        return _aggregate_from_export(
            username, secret, project_id, event_name,
            from_date, today, metric=metric, unit=unit,
        )
    except requests.exceptions.HTTPError as e:
        errors.append(f"Export API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        errors.append(f"Export API 錯誤: {e}")

    # Fallback: Segmentation API (only if the plan allows it)
    try:
        seg_type = "unique" if metric == "unique" else "general"
        return _segmentation(
            username, secret, project_id, event_name,
            from_date, today, seg_type=seg_type, unit=unit,
        )
    except requests.exceptions.HTTPError as e:
        errors.append(f"Segmentation API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        errors.append(f"Segmentation API 錯誤: {e}")

    raise RuntimeError("所有 API 端點皆無法取得資料。\n" + "\n".join(errors))


# ─────────────────────────────────────────────────────────────
# Retention — computed from Export API
# ─────────────────────────────────────────────────────────────
def _retention_from_export(username: str, secret: str, project_id: str,
                           born_event: str, return_event: Optional[str],
                           from_date: date, to_date: date,
                           unit: str = "day", interval_count: int = 14) -> pd.DataFrame:
    """Compute cohort retention client-side from Export API.

    Algorithm (Birth retention):
      1. Pull all `born_event` in the window → for each user, find their FIRST day.
         Group users by that day ⇒ cohort.
      2. Pull all `return_event` in the window → for each user, set of bucket keys.
      3. For each cohort bucket C and each offset i ∈ [0, interval_count]:
            size  = users whose first born_event bucket == C
            count = users in that cohort who had return_event at bucket C+i
            Di    = 100 * count / size
    """
    # ── 1) Born events → user → first day, grouped into cohort buckets
    user_first_day: dict[str, date] = {}
    for obj in _stream_events(username, secret, project_id, born_event, from_date, to_date):
        props = obj.get("properties", {}) or {}
        did = props.get("distinct_id")
        ts = props.get("time")
        if did is None or ts is None:
            continue
        d = datetime.utcfromtimestamp(ts).date()
        prev = user_first_day.get(did)
        if prev is None or d < prev:
            user_first_day[did] = d

    if not user_first_day:
        return pd.DataFrame(columns=["cohort", "size"])

    # Group into cohorts by bucketed first-day
    cohorts: dict[str, set] = defaultdict(set)
    for did, first in user_first_day.items():
        cohorts[_bucket_key(first, unit)].add(did)

    # ── 2) Return events → user → set of bucket keys they appeared in
    effective_return = return_event or born_event
    user_return_buckets: dict[str, set] = defaultdict(set)

    if effective_return == born_event:
        # Reuse the already-streamed data? We already consumed it. Re-stream:
        # (We only stored first_day, not every occurrence.)
        pass

    for obj in _stream_events(username, secret, project_id, effective_return, from_date, to_date):
        props = obj.get("properties", {}) or {}
        did = props.get("distinct_id")
        ts = props.get("time")
        if did is None or ts is None:
            continue
        d = datetime.utcfromtimestamp(ts).date()
        user_return_buckets[did].add(_bucket_key(d, unit))

    # ── 3) Build retention matrix
    def add_bucket(base_key: str, offset: int, unit: str) -> Optional[str]:
        """Return the bucket key that is `offset` units after `base_key`, or None if in the future."""
        base = date.fromisoformat(base_key)
        if unit == "day":
            target = base + timedelta(days=offset)
        elif unit == "week":
            target = base + timedelta(weeks=offset)
        elif unit == "month":
            # Approx: add offset months
            y = base.year + (base.month - 1 + offset) // 12
            m = (base.month - 1 + offset) % 12 + 1
            target = base.replace(year=y, month=m, day=1)
        else:
            target = base + timedelta(days=offset)
        if target > to_date:
            return None
        return _bucket_key(target, unit)

    rows = []
    for cohort_key in sorted(cohorts.keys()):
        users = cohorts[cohort_key]
        size = len(users)
        row: dict = {"cohort": cohort_key, "size": size}
        for i in range(interval_count + 1):
            target_bucket = add_bucket(cohort_key, i, unit)
            if target_bucket is None:
                row[f"D{i}"] = None  # future — not measurable yet
                continue
            count = sum(1 for did in users if target_bucket in user_return_buckets.get(did, set()))
            row[f"D{i}"] = round(100.0 * count / size, 1) if size else 0.0
        rows.append(row)

    return pd.DataFrame(rows)


def _retention_via_api(username: str, secret: str, project_id: str,
                       born_event: str, return_event: Optional[str],
                       from_date: date, to_date: date,
                       retention_type: str = "birth",
                       unit: str = "day", interval_count: int = 14) -> pd.DataFrame:
    """Official Retention API path (requires Query API access on the plan)."""
    url = "https://mixpanel.com/api/2.0/retention"
    params = {
        "project_id": project_id,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "retention_type": retention_type,
        "born_event": born_event,
        "unit": unit,
        "interval_count": interval_count,
    }
    if return_event:
        params["event"] = return_event

    resp = requests.get(
        url, params=params,
        auth=(username, secret),
        headers=_auth_headers(project_id),
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data:
        return pd.DataFrame(columns=["cohort", "size"])

    rows = []
    for cohort_date in sorted(data.keys()):
        entry = data[cohort_date] or {}
        first = entry.get("first", 0) or 0
        counts = entry.get("counts", []) or []
        row: dict = {"cohort": cohort_date, "size": int(first)}
        for i, c in enumerate(counts):
            pct = round(100.0 * c / first, 1) if first else 0.0
            row[f"D{i}"] = pct
        rows.append(row)

    return pd.DataFrame(rows)


def fetch_retention(username: str, secret: str, project_id: str,
                    born_event: str, return_event: Optional[str] = None,
                    days: int = 30, retention_type: str = "birth",
                    unit: str = "day", interval_count: int = 14) -> pd.DataFrame:
    """Fetch cohort retention.

    Tries the official Retention API first (fast, pre-aggregated). If the plan
    returns 402 / other errors, falls back to Export API + client-side cohort
    computation.

    Note: The export fallback only implements Birth retention. For Compounded
    retention the API path is required.
    """
    today = datetime.utcnow().date()
    from_date = today - timedelta(days=days)

    errors: list[str] = []

    # Try official API
    try:
        return _retention_via_api(
            username, secret, project_id,
            born_event=born_event, return_event=return_event,
            from_date=from_date, to_date=today,
            retention_type=retention_type,
            unit=unit, interval_count=interval_count,
        )
    except requests.exceptions.HTTPError as e:
        errors.append(f"Retention API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        errors.append(f"Retention API 錯誤: {e}")

    # Fallback: compute from Export (Birth retention only)
    if retention_type == "birth":
        try:
            return _retention_from_export(
                username, secret, project_id,
                born_event=born_event, return_event=return_event,
                from_date=from_date, to_date=today,
                unit=unit, interval_count=interval_count,
            )
        except requests.exceptions.HTTPError as e:
            errors.append(f"Export API {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            errors.append(f"Export 自行計算留存錯誤: {e}")

    raise RuntimeError("留存查詢失敗。\n" + "\n".join(errors))


# ─────────────────────────────────────────────────────────────
# Analytics helpers
# ─────────────────────────────────────────────────────────────
def compute_drop_vs_baseline(df: pd.DataFrame, baseline_days: int = 7) -> dict:
    """Compare today's count against the mean of the previous `baseline_days` days."""
    if df.empty or len(df) < 2:
        return {"today_count": 0, "baseline_avg": 0.0, "drop_ratio": 0.0, "has_data": False}

    df_sorted = df.sort_values("日期").reset_index(drop=True)
    today_count = int(df_sorted.iloc[-1]["事件次數"])
    baseline_window = df_sorted.iloc[-(baseline_days + 1):-1]
    if baseline_window.empty:
        return {"today_count": today_count, "baseline_avg": 0.0, "drop_ratio": 0.0, "has_data": False}

    baseline_avg = float(baseline_window["事件次數"].mean())
    drop_ratio = (today_count - baseline_avg) / baseline_avg if baseline_avg else 0.0

    return {
        "today_count": today_count,
        "baseline_avg": baseline_avg,
        "drop_ratio": drop_ratio,
        "has_data": True,
        "today_date": df_sorted.iloc[-1]["日期"],
    }


def compute_period_delta(df: pd.DataFrame, period_days: int) -> dict:
    """Compare the last `period_days` vs the previous `period_days`."""
    if df.empty or len(df) < period_days * 2:
        return {"current": 0, "previous": 0, "delta": 0, "pct": 0.0, "has_data": False}

    df_sorted = df.sort_values("日期").reset_index(drop=True)
    values = df_sorted["事件次數"].astype(float).values
    current = float(values[-period_days:].sum())
    previous = float(values[-2 * period_days:-period_days].sum())
    delta = current - previous
    pct = (delta / previous) if previous else 0.0
    return {
        "current": int(current),
        "previous": int(previous),
        "delta": int(delta),
        "pct": pct,
        "has_data": True,
    }


def compute_stickiness(username: str, secret: str, project_id: str,
                       event_name: str, days: int = 30) -> dict:
    """Compute DAU/MAU stickiness ratio.

    Uses fetch_event_data(metric='unique') which itself goes through the Export
    fallback when the plan lacks Query API access.
    """
    today = datetime.utcnow().date()
    from_date = today - timedelta(days=days)

    try:
        # Pull unique users per day (via Export fallback if needed)
        daily = fetch_event_data(
            username, secret, project_id, event_name,
            days=days, metric="unique", unit="day",
        )
    except Exception:
        return {"dau_avg": 0.0, "mau": 0, "stickiness": 0.0, "has_data": False}

    if daily.empty:
        return {"dau_avg": 0.0, "mau": 0, "stickiness": 0.0, "has_data": False}

    dau_avg = float(daily["事件次數"].mean())

    # For MAU, count unique users across the full window via Export
    try:
        unique_set: set = set()
        for obj in _stream_events(username, secret, project_id, event_name, from_date, today):
            props = obj.get("properties", {}) or {}
            did = props.get("distinct_id")
            if did is not None:
                unique_set.add(did)
        mau = len(unique_set)
    except Exception:
        mau = int(daily["事件次數"].sum())  # upper-bound fallback

    stickiness = (dau_avg / mau) if mau else 0.0
    return {"dau_avg": dau_avg, "mau": mau, "stickiness": stickiness, "has_data": True}
