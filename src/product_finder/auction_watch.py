"""End-of-auction price capture.

A live auction's price is just a bid until it closes (see
scoring.is_live_auction() for why that's never trusted for scoring). But the
closing price *is* a genuinely useful "sold for" proxy for the used-price
index — eBay's Marketplace Insights (sold-price) API isn't available to
this app (tested directly, see deal-scoring notes), so this is the
alternative: track an auction as it nears its end and capture the price the
moment it closes.

Polling cadence is tiered so we don't hammer the API for auctions that are
hours away, but do check tightly in the closing seconds/minutes, since
that's when bidding activity (and therefore the price) concentrates:

    > 10 min remaining  -> poll at most every 5 min
    2-10 min remaining  -> poll at most every 1 min
    < 2 min remaining   -> poll at most every 20 sec

This module only knows how to drive that loop and record the result; the
CLI's `watch` loop is what actually calls poll_and_capture() on a tight
tick (see cli.py) — there's no separate background process.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db, sources
from .config import AppConfig
from .sources.ebay import EbaySource

log = logging.getLogger(__name__)

# (remaining-time threshold, poll cadence) — first match wins, checked in
# ascending threshold order. None as a threshold means "everything larger".
_CADENCE_TIERS: list[tuple[timedelta | None, timedelta]] = [
    (timedelta(minutes=2), timedelta(seconds=20)),
    (timedelta(minutes=10), timedelta(minutes=1)),
    (None, timedelta(minutes=5)),
]

# If we still can't get a confirmed "ended" read this long after end_time
# (item removed, API error, etc.), stop trying rather than poll forever.
GIVE_UP_AFTER = timedelta(minutes=10)


def _cadence_for(remaining: timedelta) -> timedelta:
    for threshold, cadence in _CADENCE_TIERS:
        if threshold is None or remaining <= threshold:
            return cadence
    return _CADENCE_TIERS[-1][1]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def due_for_poll(end_time: datetime, last_poll_at: datetime | None, now: datetime) -> bool:
    """Whether a tracked auction should be polled right now, given its
    tiered cadence. Auctions already past end_time use the tightest tier —
    we want to confirm the close quickly, not wait out a stale schedule."""
    if last_poll_at is None:
        return True
    return now - last_poll_at >= _cadence_for(end_time - now)


def poll_and_capture(cfg: AppConfig, conn: sqlite3.Connection) -> int:
    """Poll auctions due for a check, record every poll as a snapshot
    observation (see db.record_auction_snapshot), and record the closing
    price for any that have ended since we last looked. Returns how many
    closes were captured this call (snapshot recording itself isn't counted
    — this return value is what callers already use for close-capture
    logging). Safe to call often — most calls find nothing due.

    Tracking no longer requires a catalogue-product match (db.list_tracked_auctions
    was broadened) — every live auction gets its snapshot history recorded.
    The product's used-price observation on close still only happens for
    listings actually matched to a product (product_id is None otherwise)."""
    eff_cfg = db.effective_config(conn, cfg)
    source = sources.build_registry(eff_cfg).get("ebay")
    if not isinstance(source, EbaySource) or not source.is_automated():
        return 0

    now = datetime.now(timezone.utc)
    captured = 0
    for row in db.list_tracked_auctions(conn):
        if "AUCTION" not in json.loads(row["buying_options"] or "[]"):
            continue
        end_time = _parse(row["end_time"])
        last_poll_at = _parse(row["last_poll_at"]) if row["last_poll_at"] else None
        if not due_for_poll(end_time, last_poll_at, now):
            continue

        try:
            snapshot = source.get_item(row["external_id"])
        except Exception as exc:
            log.warning("Auction poll failed for %s: %s", row["external_id"], exc)
            continue

        db.mark_listing_polled(conn, row["id"])
        if snapshot is not None:
            # Record this observation regardless of ended/not-ended — this is
            # what builds the snapshot history for trajectory scoring, not
            # just the closing capture below.
            db.record_auction_snapshot(
                conn,
                row["id"],
                source=source.name,
                current_bid_price=snapshot.current_bid,
                currency=snapshot.currency,
                bid_count=snapshot.bid_count,
                buy_it_now_price=snapshot.buy_it_now_price,
                shipping_price=snapshot.shipping_price,
                end_time=row["end_time"],
                watch_count=snapshot.watch_count,
                view_count=snapshot.view_count,
                raw_payload=snapshot.raw,
            )
        if snapshot is not None and snapshot.ended:
            if row["product_id"] is not None:
                db.record_price_observation(
                    conn, row["product_id"], snapshot.price, source=f"{source.name}-close"
                )
            db.mark_sold_captured(conn, row["id"])
            captured += 1
        elif now - end_time > GIVE_UP_AFTER:
            # Can't confirm a close (item gone, repeated API errors) —
            # give up rather than poll this one forever.
            db.mark_sold_captured(conn, row["id"])
    return captured
