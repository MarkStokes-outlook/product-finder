"""Webhook alerts: POST a JSON payload per new match. Failures never crash a run."""

from __future__ import annotations

import logging

import requests

from ..models import MatchAlert

log = logging.getLogger(__name__)


def send(alert: MatchAlert, url: str) -> bool:
    payload = {
        "project": alert.project_name,
        "item": alert.item_name,
        "title": alert.listing.title,
        "price": alert.listing.price,
        "currency": alert.listing.currency,
        "normal_price": alert.normal_price,
        "target_deal_price": alert.target_deal_price,
        "margin_abs": alert.evaluation.margin_abs,
        "margin_pct": alert.evaluation.margin_pct,
        "under_target": alert.evaluation.under_target,
        "grade": alert.evaluation.grade,
        "flags": alert.evaluation.flags,
        "deal_score": alert.evaluation.deal_score,
        "source": alert.listing.source,
        "url": alert.listing.url,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.warning("Webhook alert failed: %s", exc)
        return False
