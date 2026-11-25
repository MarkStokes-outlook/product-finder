"""Seasonal new-retail price intelligence for existing wanted catalogue items.

Historical observations are domain-scoped, not exact-URL or stock evidence.
Never use MSRP, configured normal prices or unapproved search candidates
as proof of a discount. No changes to marketplace scoring or alert policy.
"""
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from statistics import median
from urllib.parse import urlsplit

from . import db

FRESH_HOURS = 24
BASELINE_DAYS = 60
MIN_DAYS = 3
MIN_SPAN_DAYS = 7


def event_dates(year):
    last = date(year, 11, 30)
    friday = last - timedelta(days=(last.weekday() - 4) % 7)
    return friday, friday - timedelta(days=7), friday + timedelta(days=3)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _price(value):
    return value if isinstance(value, (float, int)) and isfinite(value) and value > 0 else None


def _domain(url):
    try:
        parsed = urlsplit(url or "")
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return parsed.hostname.lower().removeprefix("www.")
    except ValueError:
        return None


def assess(row, history, now):
    """Return price-only evidence and a conservative, deterministic verdict."""
    _, start, _ = event_dates(now.year)
    cutoff = min(start, now.date())
    first = cutoff - timedelta(days=BASELINE_DAYS)
    domain = _domain(row["canonical_price_url"])
    daily = {}
    for observation in history:
        ts = _timestamp(observation["observed_at"])
        price = _price(observation["price"])
        if (domain and observation["domain"].lower().removeprefix("www.") == domain
                and ts and first <= ts.date() < cutoff and price):
            daily.setdefault(ts.date(), []).append(price)

    baseline = None
    if len(daily) >= MIN_DAYS and (max(daily) - min(daily)).days >= MIN_SPAN_DAYS:
        baseline = median([median(prices) for prices in daily.values()])

    checked = _timestamp(row["last_price_check_at"])
    current = _price(row["typical_new_price"])
    fresh = bool(domain and current and checked and row["last_price_check_ok"]
                 and timedelta(0) <= now - checked <= timedelta(hours=FRESH_HOURS))
    target = _price(row["target_deal_price"])
    budget = _price(row["max_price"])
    saving = baseline - current if fresh and baseline is not None else None
    percent = saving / baseline * 100 if saving is not None else None
    if not domain:
        verdict = "Needs retailer approval"
    elif not fresh:
        verdict = "Price needs refresh"
    elif budget is not None and current > budget:
        verdict = "Above budget"
    elif baseline is None:
        verdict = "Insufficient history"
    elif percent >= 20:
        verdict = "Strong observed reduction"
    elif percent >= 10:
        verdict = "Observed reduction"
    elif percent > 0:
        verdict = "Small observed reduction"
    else:
        verdict = "No observed reduction"

    return {
        "domain": domain, "url": row["canonical_price_url"] if domain else None,
        "current": current if fresh else None, "checked": checked, "baseline": baseline,
        "sample_days": len(daily), "saving": saving, "percent": percent,
        "target": target, "budget": budget, "verdict": verdict,
        "target_met": bool(fresh and target is not None and current <= target),
        "above_budget": bool(fresh and budget is not None and current > budget),
    }


def dashboard(conn, now=None):
    now = now or datetime.now(timezone.utc)
    friday, start, end = event_dates(now.year)
    rows = conn.execute("""
        SELECT ip.id AS tracking_id, p.id AS product_id, p.manufacturer, p.model,
               p.canonical_price_url, p.typical_new_price, p.last_price_check_at,
               p.last_price_check_ok, pr.id AS project_id, pr.name AS project,
               i.name AS item, i.max_price,
               COALESCE(ip.target_deal_price, i.target_deal_price) AS target_deal_price
        FROM item_products ip
        JOIN products p ON p.id = ip.product_id
        JOIN items i ON i.id = ip.item_id
        JOIN projects pr ON pr.id = i.project_id
        WHERE ip.wanted = 1 AND ip.archived = 0 AND i.archived = 0 AND pr.archived = 0
    """).fetchall()
    histories = {}
    deals = []
    for row in rows:
        pid = row["product_id"]
        if pid not in histories:
            histories[pid] = db.list_new_price_history(conn, pid)
        deals.append({**dict(row), **assess(row, histories[pid], now)})
    # Verified recent *price observations* with positive savings first;
    # above-budget and unknown/stale observations never outrank them.
    def rank(deal):
        pct = deal["percent"]
        return (deal["above_budget"], pct is None, -(pct or 0),
                deal["project"], deal["manufacturer"], deal["tracking_id"])
    deals.sort(key=rank)
    return {"deals": deals, "friday": friday, "start": start, "end": end,
            "active": start <= now.date() <= end, "year": now.year}
